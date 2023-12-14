import functools
import gettext
import json
import os
import shutil

import sdl2
import sdl2.ext

import harbourmaster
import pySDL2gui

from loguru import logger


_ = gettext.gettext


class StringFormatter:
    def __init__(self, data_dict):
        self.data_dict = data_dict

    @functools.lru_cache(1024)
    def parse_text(self, text):
        result = []
        current = ''
        while len(text) > 0:
            before, bracket, text = text.partition('{')
            current += before

            if bracket == '' or text == '':
                break

            elif text.startswith('{'):
                current += '{'
                text = text[1:]
                continue

            else:
                token, bracket, text = text.partition('}')
                if bracket == '':
                    current += token
                    break

                if token == '':
                    current += '{}'
                    continue

                result.append((current, token))
                current = ''

        if current != '':
            result.append((current, None))

        return tuple(result)

    def execute_if(self, text, keys_used=None):
        do_not = False

        if text.startswith('!'):
            do_not = True
            text = text[1:]

        if ':' in text:
            key, value = text.split(':', 1)

            if keys_used is not None and key not in keys_used:
                keys_used.append(key)

            if value.startswith(':'):
                value = value[1:]
                if keys_used is not None and value not in keys_used:
                    keys_used.append(value)

                value = self.data_dict.get(value, '')

            result = self.data_dict.get(key, '') == value
        else:
            if keys_used is not None and text not in keys_used:
                keys_used.append(text)

            result = self.data_dict.get(text, '') not in ('', 'None')

        if do_not:
            result = not result

        return result

    def format_string(self, text, keys_used=None):
        output = []
        stack = [True]

        # TRANSLATIONS :D
        text = gettext.dgettext('themes', text)

        for before, key in self.parse_text(text):
            if stack[-1] and before != '':
                output.append(before)

            if key is not None:
                if key == 'else':
                    stack[-1] = not stack[-1]

                elif key == 'endif':
                    if len(stack) > 1:
                        stack.pop(-1)

                elif key.startswith('if:'):
                    if_key = key[3:]

                    if self.execute_if(if_key, keys_used):
                        stack.append(True)
                    else:
                        stack.append(False)

                else:
                    if keys_used is not None and key not in keys_used:
                        keys_used.append(key)

                    value = self.data_dict.get(key, f'{{{key}}}')
                    if stack[-1]:
                        output.append(value)

        return ''.join(output)


class BaseScene:
    """
    Scenes handle drawing / logic, different scenes can be transitioned to and or layered.

    Only the top layer receives events.
    """

    def __init__(self, gui):
        self.gui = gui
        self.tags = {}
        self.config = {}
        self.regions = []
        self.text_regions = {}
        self.bar_regions = {}
        self.image_regions = {}
        self.update_regions = {}
        self.music = None
        self.music_volume = 128
        self.active = False
        self.scene_title = ""
        self.scene_tooltip = ""

    def scene_deactivate(self):
        self.active = False

    def scene_activate(self):
        if not self.active:
            self.active = True
            self.gui.sounds.easy_music(self.music, volume=max(0, min(self.music_volume, 128)))
            self.gui.set_data("scene.title", self.scene_title)
            self.gui.set_data("scene.tooltip", self.scene_tooltip)

    def set_tooltip(self, text):
        self.scene_tooltip = text
        self.gui.set_data("scene.tooltip", text)

    def load_regions(self, section, required_tags):
        rects = self.gui.new_rects()
        temp_required_tags = list(required_tags)

        for number, (region_name, region_data) in enumerate(self.gui.theme_data[section].items()):
            if region_name == "#config":
                self.config = region_data
                continue

            if "music" in region_data:
                self.music = region_data["music"]
                self.music_volume = region_data.get("music-volume", 128)

            # print(f"Loading region {region_name}: {region_data}")
            region = pySDL2gui.Region(self.gui, region_data, region_name, number, rects)

            if "image" in region_data and "{" in region_data["image"]:
                image_keys = []
                region.image = self.gui.images.load(self.gui.format_data(region_data["image"], image_keys))

                self.image_regions[region_name] = (region, region_data["image"])
                for key in image_keys:
                    self.update_regions.setdefault(key, []).append(region_name)

            if "text" in region_data and "{" in region_data["text"]:
                text_keys = []
                region.text = self.gui.format_data(region_data["text"], text_keys)

                self.text_regions[region_name] = (region, region_data["text"])
                for key in text_keys:
                    self.update_regions.setdefault(key, []).append(region_name)

            if "bar" in region_data:
                found = False
                text_keys = []
                bar_copy = region_data["bar"][:]
                for i in range(len(region_data["bar"])):
                    bar_item = region_data["bar"][i]
                    if not isinstance(bar_item, str) or "{" not in bar_item:
                        continue

                    found = True
                    region_data["bar"][i] = self.gui.format_data(bar_item, text_keys)

                if found:
                    self.bar_regions[region_name] = (region, bar_copy)
                    for key in text_keys:
                        self.update_regions.setdefault(key, []).append(region_name)

            region_tag = region_data.get("tag", region_name)
            if region_tag is not None:
                self.tags[region_tag] = region

            if region_tag in temp_required_tags:
                temp_required_tags.remove(region_tag)

            self.regions.append(region)

        if len(temp_required_tags) > 0:
            logger.error(f"Error: missing one or more tags for section {section}: {', '.join(temp_required_tags)}")
            raise RuntimeError("Error missing section tag in theme")

        self.regions.sort(key=lambda x: (x.z_index, x.z_position))

        if "buttons" in self.config:
            if "A" in self.config["buttons"]:
                del self.config["buttons"]["A"]

            if "B" in self.config["buttons"]:
                del self.config["buttons"]["B"]

            if "X" in self.config["buttons"]:
                del self.config["buttons"]["X"]

            if "Y" in self.config["buttons"]:
                del self.config["buttons"]["Y"]

    def update_data(self, keys):
        regions = set()

        for key in keys:
            if key in self.update_regions:
                regions.update(self.update_regions[key])

        for region_name in regions:
            if region_name in self.image_regions:
                region, text = self.image_regions[region_name]
                new_image = self.gui.format_data(text)
                # print(f"Loading image {region} -> {text} -> {new_image}")
                region.image = self.gui.images.load(new_image)
                self.gui.updated = True

            if region_name in self.text_regions:
                region, text = self.text_regions[region_name]
                region.text = self.gui.format_data(text)
                self.gui.updated = True

            elif region_name in self.bar_regions:
                region, bar = self.bar_regions[region_name]
                new_bar = bar[:]

                for i, bar_item in enumerate(bar):                    
                    if not isinstance(bar_item, str) or "{" not in bar_item:
                        continue

                    new_bar[i] = self.gui.format_data(bar_item)

                region.bar = new_bar
                self.gui.updated = True

    def do_update(self, events):
        for region in self.regions:
            # print(f"DRAW {region}")
            region.update()

        return False

    def do_draw(self):
        for region in self.regions:
            # print(f"DRAW {region}")
            if not region.visible:
                continue

            region.draw()

    def set_buttons(self, key_map):
        key_to_image = {
            'A':     '_A',
            'B':     '_B',
            'X':     '_X',
            'Y':     '_Y',
            'UP':    '_UP',
            'DOWN':  '_DOWN',
            'LEFT':  '_LEFT',
            'RIGHT': '_RIGHT',
            'START': '_START',
            'SELECT': '_SELECT',
            'L': '_L',
            'R': '_R',
            }

        if 'button_bar' not in self.tags:
            return

        if len(key_map) == 0:
            self.tags['button_bar'].bar = None
            return

        actions = {}

        for key, action in key_map.items():
            actions.setdefault(action, []).append(key_to_image.get(key, key))

        output = []
        for action, key in actions.items():
            output.extend(key)
            output.append(action)

        # print(f"-> {key_map} = {output}")

        self.tags['button_bar'].bar = output

    def button_activate(self):
        if 'button_bar' not in self.tags:
            return

        self.gui.sounds.play(self.tags['button_bar'].button_sound, volume=self.tags['button_bar'].button_sound_volume)

    def button_back(self):
        if 'button_bar' not in self.tags:
            return

        if self.tags['button_bar'].button_sound_alt is None:
            self.button_activate()

        else:
            self.gui.sounds.play(self.tags['button_bar'].button_sound_alt, volume=self.tags['button_bar'].button_sound_alt_volume)

    def config_buttons(self, events):
        if "buttons" not in self.config:
            return

        for button, action in self.config.get("buttons", {}).items():
            if events.was_pressed(button):
                yield action


class BlankScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)

        self.load_regions("blank", [])
        self.set_buttons({})


class MainMenuScene(BaseScene):
    KONAMI_CODE = ('UP', 'UP', 'DOWN', 'DOWN', 'LEFT', 'RIGHT', 'LEFT', 'RIGHT', 'B', 'A', 'START', 'DONE')

    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Main Menu")

        self.load_regions("main_menu", ['option_list'])

        self.tags['option_list'].reset_options()
        self.tags['option_list'].add_option(
            ('featured-ports', None),
            _('Featured Ports'),
            description=_("Hand curated lists of ports"))
        self.tags['option_list'].add_option(
            ('install', []),
            _("All Ports"),
            description=_("List all ports available on PortMaster."))
        self.tags['option_list'].add_option(
            ('install', ['rtr']),
            _("Ready to Run Ports"),
            description=_("List all ports that are ready to play!"))
        self.tags['option_list'].add_option(
            ('uninstall', ['installed']),
            _("Manage Ports"),
            description=_("Update / Uninstall Ports"))

        self.tags['option_list'].add_option(None, "")
        self.tags['option_list'].add_option(
            ('options', None),
            _("Options"),
            description=_("PortMaster Options"))
        self.tags['option_list'].add_option(
            ('exit', None),
            _("Exit"),
            description=_("Quit PortMaster"))

        self.set_buttons({'A': _('Enter'), 'B': _('Quit')})
        self.detecting_konami = 0
        self.last_selected = None

    def do_update(self, events):
        super().do_update(events)

        if events.was_pressed(self.KONAMI_CODE[self.detecting_konami]):
            self.detecting_konami += 1

            if self.KONAMI_CODE[self.detecting_konami] == 'DONE':
                self.gui.hm.cfg_data['konami'] = not self.gui.hm.cfg_data.get('konami', False)
                self.gui.hm.save_config()
                self.detecting_konami = 0
                self.gui.message_box(_('Secret Mode {secret_mode}').format(
                    secret_mode=(self.gui.hm.cfg_data['konami'] and _('Enabled') or _('Disabled'))))

                return True

            if self.KONAMI_CODE[self.detecting_konami-1] in ('B', 'A', 'START'):
                return True

        elif events.any_pressed():
            self.detecting_konami = 0

        if self.last_selected != self.tags['option_list'].selected_option():
            self.set_tooltip(self.tags['option_list'].selected_description())
            self.last_selected = self.tags['option_list'].selected_option()

        if events.was_pressed('A'):
            selected_option, selected_parameter = self.tags['option_list'].selected_option()

            self.button_activate()

            if selected_option in ('install', 'uninstall'):
                self.gui.push_scene('ports', PortsListScene(self.gui, {'mode': selected_option, 'base_filters': selected_parameter}))
                return True

            elif selected_option == 'featured-ports':
                self.gui.push_scene('featured-ports', FeaturedPortsListScene(self.gui))
                return True

            elif selected_option == 'options':
                self.gui.push_scene('option', OptionScene(self.gui))
                return True

            elif selected_option == 'exit':
                self.gui.do_cancel()
                return True

        elif events.was_pressed('B'):
            self.button_back()
            if self.gui.message_box(
                    _("Are you sure you want to exit PortMaster?"),
                    want_cancel=True):

                self.gui.do_cancel()
                return True


class OptionScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Options Menu")

        self.load_regions("option_menu", ['option_list'])

        self.tags['option_list'].reset_options()

        self.tags['option_list'].add_option(None, _("Interface"))

        self.tags['option_list'].add_option(
            'select-language',
            _("Choose Language"),
            description=_("Select the language PortMaster uses."))
        self.tags['option_list'].add_option(
            'select-theme',
            _("Select Theme"),
            description=_("Select a theme for PortMaster."))

        schemes = self.gui.themes.get_theme_schemes_list()
        if len(schemes) > 1:
            self.tags['option_list'].add_option(
                'select-scheme',
                _("Select Color Scheme"),
            description=_("Select a colour scheme for PortMaster"))

        self.tags['option_list'].add_option(None, _("Audio"))

        self.tags['option_list'].add_option(
            'toggle-music',
            _("Music: ") + (self.gui.sounds.music_is_disabled and _("Disabled") or _("Enabled")),
            description=_("Enable or Disable background music in PortMaster."))
        self.tags['option_list'].add_option(
            'toggle-sfx',
            _("Sound FX: ") + (self.gui.sounds.sound_is_disabled and _("Disabled") or _("Enabled")),
            description=_("Enable or Disable soundfx in PortMaster."))

        self.tags['option_list'].add_option(None, _("System"))

        self.tags['option_list'].add_option(
            'runtime-manager',
            _("Runtime Manager"),
            description=_("Manage port runtimes."))

        self.tags['option_list'].add_option(
            'update-ports',
            _("Update Ports"),
            description=_("Update all ports and associated information"))

        self.tags['option_list'].add_option(
            'update-portmaster',
            _("Update PortMaster"),
            description=_("Force check for a new PortMaster version."))

        self.tags['option_list'].add_option(
            'release-channel',
            _("Release Channel: {channel}").format(
                channel=self.gui.hm.cfg_data.get('release_channel', "stable")),
            description=_("Change release channel of PortMaster, either beta or stable."))

        if len(self.gui.hm.get_gcd_modes()) > 0:
            gcd_mode = self.gui.hm.get_gcd_mode()
            self.tags['option_list'].add_option(
                'toggle-gcd',
                _("Controller Mode: {controller_mode}").format(controller_mode=gcd_mode),
                description=_("Toggle between various controller layouts."))

        if self.gui.hm.cfg_data.get('konami', False):
            self.tags['option_list'].add_option(None, _("Secret Options"))
            self.tags['option_list'].add_option(
                'delete-config',
                _("Delete PortMaster Config"),
                description=_("This can break stuff, don't touch unless you know what you are doing."))
            self.tags['option_list'].add_option(
                'delete-runtimes',
                _("Delete PortMaster Runtimes"),
                description=_("This can break stuff, don't touch unless you know what you are doing."))

        # self.tags['option_list'].add_option(None, "")
        # self.tags['option_list'].add_option('back', _("Back"))
        self.tags['option_list'].list_select(0)
        self.set_buttons({'A': _('Enter'), 'B': _('Back')})
        self.last_selected = None

    def do_update(self, events):
        super().do_update(events)

        if self.last_selected != self.tags['option_list'].selected_option():
            self.set_tooltip(self.tags['option_list'].selected_description())
            self.last_selected = self.tags['option_list'].selected_option()

        if events.was_pressed('A'):
            selected_option = self.tags['option_list'].selected_option()

            self.button_activate()

            # print(f"Selected {selected_option}")

            if selected_option == 'update-ports':
                self.gui.do_update_ports()
                return True

            if selected_option == 'update-portmaster':
                self.gui.hm.cfg_data['update_checked'] = None
                self.gui.hm.save_config()
                self.gui.events.running = False

                if not harbourmaster.HM_TESTING:
                    reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                    if not reboot_file.is_file():
                        reboot_file.touch(0o644)

                return True

            if selected_option == 'release-channel':
                channel_change = {
                    "stable": "beta",
                    "beta": "stable",
                    }
                current_channel = self.gui.hm.cfg_data.get('release_channel', "stable")
                new_channel = channel_change[current_channel]

                if self.gui.message_box(
                        _("Are you sure you want to change the release channel from {current_channel} to {new_channel}?\n\nPortMaster will upgrade or downgrade accordingly.").format(
                            current_channel=current_channel,
                            new_channel=new_channel,
                            ),
                        want_cancel=True):

                    self.gui.hm.cfg_data['release_channel'] = new_channel
                    self.gui.hm.cfg_data['update_checked'] = None
                    self.gui.hm.save_config()
                    self.gui.events.running = False

                    if not harbourmaster.HM_TESTING:
                        reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                        if not reboot_file.is_file():
                            reboot_file.touch(0o644)

                return True

            if selected_option == 'toggle-music':
                self.gui.hm.cfg_data['music-disabled'] = self.gui.sounds.music_is_disabled = not self.gui.sounds.music_is_disabled
                self.gui.hm.save_config()

                item = self.tags['option_list'].list_selected()
                self.tags['option_list'].list[item] = (
                    _("Music: ") + (self.gui.sounds.music_is_disabled and _("Disabled") or _("Enabled")))
                return True

            if selected_option == 'toggle-sfx':
                self.gui.hm.cfg_data['sfx-disabled'] = self.gui.sounds.sound_is_disabled = not self.gui.sounds.sound_is_disabled
                self.gui.hm.save_config()

                item = self.tags['option_list'].list_selected()
                self.tags['option_list'].list[item] = (
                    _("Sound FX: ") + (self.gui.sounds.sound_is_disabled and _("Disabled") or _("Enabled")))
                return True

            if selected_option == 'toggle-gcd':
                gcd_modes = self.gui.hm.get_gcd_modes()
                if len(gcd_modes) == 0:
                    return True

                gcd_mode = self.gui.hm.get_gcd_mode()
                if gcd_mode not in gcd_modes:
                    gcd_mode = gcd_modes[0]
                else:
                    gcd_mode = gcd_modes[(gcd_modes.index(gcd_mode) + 1) % len(gcd_modes)]

                self.gui.hm.set_gcd_mode(gcd_mode)

                item = self.tags['option_list'].list_selected()
                self.tags['option_list'].list[item] = (
                    _("Controller Mode: {controller_mode}").format(controller_mode=gcd_mode))

                return True

            if selected_option == 'runtime-manager':
                self.gui.push_scene('runtime-manager', RuntimesScene(self.gui))
                return True

            if selected_option == 'source-manager':
                self.gui.push_scene('source-manager', SourceScene(self.gui))
                return True

            if selected_option == 'keyboard':
                self.gui.push_scene('osk', OnScreenKeyboard(self.gui))
                return True

            if selected_option == 'select-theme':
                self.gui.push_scene('select-theme', ThemesScene(self.gui))
                return True

            if selected_option == 'select-scheme':
                self.gui.push_scene('select-scheme', ThemeSchemeScene(self.gui))
                return True

            if selected_option == 'select-language':
                self.gui.push_scene('select-language', LanguageScene(self.gui))
                return True

            ## Secret options
            if selected_option == 'delete-config':
                self.gui.events.running = False

                shutil.rmtree(harbourmaster.HM_TOOLS_DIR / "PortMaster" / "config")

                if not harbourmaster.HM_TESTING:
                    reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                    if not reboot_file.is_file():
                        reboot_file.touch(0o644)

                return True

            if selected_option == 'delete-runtimes':
                runtimes = list((harbourmaster.HM_TOOLS_DIR / "PortMaster" / "libs").glob("*.squashfs"))
                if len(runtimes) == 0:
                    self.gui.message_box("No runtimes found.")
                    return True

                with self.gui.enable_cancellable(False):
                    with self.gui.enable_messages():
                        self.gui.message(_("Deleting Runtimes:"))
                        self.gui.do_loop()

                        for runtime_file in runtimes:
                            logger.info(f"removing {runtime_file}")
                            self.gui.message(f"- {runtime_file}")
                            runtime_file.unlink()
                            self.gui.do_loop()

                self.gui.message_box(f"Removed {len(runtimes)} runtimes.")
                return True

            if selected_option == 'back':
                self.gui.pop_scene()
                return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class SourceScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Source Manager")

        self.load_regions("option_list", ['option_list', ])


class RuntimesScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Runtime Manager")

        self.load_regions("runtime_list", ['runtime_list', ])
        self.update_runtimes()

    def update_runtimes(self):
        runtimes = []
        self.runtimes = {}
        download_size = {}
        total_size = 0

        for source_prefix, source in self.gui.hm.sources.items():
            for runtime in source.utils:
                if runtime not in runtimes:
                    runtimes.append(runtime)
                    download_size[runtime] = source._data[runtime]["size"]
                    total_size += download_size[runtime]

        runtimes.sort(key=lambda name: harbourmaster.runtime_nicename(runtime))

        download_size['all'] = total_size
        runtimes.append('all')

        self.tags['runtime_list'].reset_options()
        all_installed = True

        for runtime in runtimes:
            if runtime == "all":
                self.runtimes[runtime] = {
                    'name': _("Download All"),
                    'installed': None,
                    'file': None,
                    'ports': [],
                    'download_size': download_size[runtime],
                    'disk_size': 0,
                    }

                self.tags['runtime_list'].add_option(None, "")

            else:
                self.runtimes[runtime] = {
                    'name': harbourmaster.runtime_nicename(runtime),
                    'installed': None,
                    'file': (self.gui.hm.libs_dir / runtime),
                    'ports': [],
                    'download_size': download_size[runtime],
                    'disk_size': 0,
                    }

                self.runtimes[runtime]['installed'] = self.runtimes[runtime]['file'].is_file()

                if self.runtimes[runtime]['file'].is_file():
                    self.runtimes[runtime]['disk_size'] = self.runtimes[runtime]['file'].stat().st_size

                else:
                    all_installed = False

            self.tags['runtime_list'].add_option(runtime, self.runtimes[runtime]['name'])

        self.runtimes['all']['installed'] = all_installed

        for port_name, port_info in self.gui.hm.list_ports(filters=['installed']).items():
            if port_info['attr']['runtime'] in self.runtimes:
                self.runtimes[port_info['attr']['runtime']]['ports'].append(port_info['attr']['title'])

        self.last_select = self.tags['runtime_list'].selected_option()
        self.last_verified = None
        self.update_selection()

    def update_selection(self):
        runtime_info = self.runtimes[self.last_select]

        self.gui.set_data('runtime_info.name', runtime_info['name'])
        self.gui.set_data('runtime_info.status', runtime_info['installed'] and _('Installed') or _('Not Installed'))
        self.gui.set_data('runtime_info.in_use', len(runtime_info['ports']) > 0 and _('Used') or _('Not Used'))
        self.gui.set_data('runtime_info.ports', harbourmaster.oc_join(runtime_info['ports']))
        self.gui.set_data('runtime_info.download_size', harbourmaster.nice_size(runtime_info['download_size']))

        if runtime_info['installed']:
            self.gui.set_data('runtime_info.disk_size', harbourmaster.nice_size(runtime_info['disk_size']))

        else:
            self.gui.set_data('runtime_info.disk_size', "")

        # self.gui.set_data('runtime_info.verified', "To be done.")

        # self.gui.set_runtime_info(self.last_select, theme_info)

        if runtime_info['installed']:
            buttons = {'A': _('Check'), 'Y': _('Uninstall'), 'B': _('Back')}

        else:
            buttons = {'A': _('Install'), 'B': _('Back')}

        self.set_buttons(buttons)

    def do_update(self, events):
        super().do_update(events)
        selected = self.tags['runtime_list'].selected_option()

        if selected != self.last_select:
            self.last_select = selected
            self.update_selection()

        if events.was_pressed('A'):
            self.button_activate()
            if selected == 'all':
                if self.gui.message_box(_("Are you sure you want to download and verify all runtimes?"), want_cancel=True):
                    with self.gui.enable_cancellable(False):
                        with self.gui.enable_messages():
                            for runtime in self.runtimes:
                                if runtime == 'all':
                                    continue

                                self.gui.do_runtime_check(runtime, in_install=True)

                                if self.runtimes[runtime]['file'].is_file():
                                    self.runtimes[runtime]['disk_size'] = self.runtimes[runtime]['file'].stat().st_size
                                    self.runtimes[runtime]['verified'] = "Verified"
                                else:
                                    self.runtimes[runtime]['disk_size'] = ""
                                    self.runtimes[runtime]['verified'] = ""

                                self.update_runtimes()

            else:
                self.gui.do_runtime_check(selected)
                self.runtimes[selected]['installed'] = self.runtimes[selected]['file'].is_file()

                if self.runtimes[selected]['file'].is_file():
                    self.runtimes[selected]['disk_size'] = self.runtimes[selected]['file'].stat().st_size
                    self.runtimes[selected]['verified'] = "Verified"
                else:
                    self.runtimes[selected]['disk_size'] = ""
                    self.runtimes[selected]['verified'] = ""

                self.update_runtimes()

            self.last_select = None

        if events.was_pressed('Y'):
            if selected != 'all' and self.runtimes[selected]['file'].is_file():
                self.button_activate()

                self.runtimes[selected]['file'].unlink()
                self.runtimes[selected]['installed'] = False
                self.runtimes[selected]['disk_size'] = ""
                self.runtimes[selected]['verified'] = ""

                self.gui.message_box(_("Deleted runtime {runtime}").format(
                    runtime=self.runtimes[selected]['name']))

                self.last_select = None
                self.update_runtimes()

            return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class ThemesScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Select Theme")

        self.load_regions("themes_list", ['themes_list', ])

        if self.gui.theme_downloader is None:
            import pugtheme
            with self.gui.enable_cancellable(False):
                with self.gui.enable_messages():
                    self.gui.theme_downloader = pugtheme.ThemeDownloader(self.gui, self.gui.themes)

        self.themes = self.gui.themes.get_themes_list(
            self.gui.theme_downloader.get_theme_list())

        selected_theme = self.gui.hm.cfg_data['theme']

        self.tags['themes_list'].reset_options()
        for theme_name, theme_data in self.themes.items():
            if theme_name == selected_theme:
                self.tags['themes_list'].add_option(theme_name, _("{theme_name} (Selected)").format(theme_name=theme_data['name']))

            else:
                self.tags['themes_list'].add_option(theme_name, theme_data['name'])

        self.last_select = self.tags['themes_list'].selected_option()
        self.update_selection()

    def update_selection(self):
        theme_info = self.themes[self.last_select]
        self.gui.set_theme_info(self.last_select, theme_info)

        keys = {}
        if theme_info['status'] in ("Installed", "Update Available"):
            keys['A'] = _('Select')

        keys['B'] = _('Back')

        if theme_info['url'] is not None:
            keys['X'] = _('Download')

        self.set_buttons(keys)

    def do_update(self, events):
        super().do_update(events)

        if self.tags['themes_list'].selected_option() != self.last_select:
            self.last_select = self.tags['themes_list'].selected_option()
            self.update_selection()

        if events.was_pressed('A'):
            theme_info = self.themes[self.last_select]

            if theme_info['status'] not in ("Installed", "Update Available"):
                return True

            self.button_activate()

            if self.gui.message_box(_("Do you want to change theme?\n\nYou will have to restart for it to take affect."), want_cancel=True):
                self.gui.hm.cfg_data['theme'] = self.last_select
                self.gui.hm.cfg_data['theme-scheme'] = None
                self.gui.hm.save_config()
                self.gui.events.running = False

                if not harbourmaster.HM_TESTING:
                    reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                    if not reboot_file.is_file():
                        reboot_file.touch(0o644)

                return True

        if events.was_pressed('X'):
            theme_info = self.themes[self.last_select]

            if theme_info['url'] is None:
                return True

            self.button_activate()

            with self.gui.enable_cancellable(True):
                with self.gui.enable_messages():
                    self.gui.do_install(theme_info['name'], theme_info['url'] + ".md5")

                    self.themes = self.gui.themes.get_themes_list(
                        self.gui.theme_downloader.get_theme_list())

                    self.update_selection()

            return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class ThemeSchemeScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Select Colour Scheme")

        self.load_regions("option_menu", ['option_list', ])

        theme_name = self.gui.themes.get_current_theme()
        schemes = self.gui.themes.get_theme_schemes_list()

        default_scheme = self.gui.themes.get_theme(theme_name).theme_data.get("#info", {}).get("default-scheme", None)
        selected_scheme = self.gui.hm.cfg_data.get('theme-scheme', default_scheme)

        self.tags['option_list'].reset_options()
        for scheme_name in schemes:
            if selected_scheme is None or scheme_name == selected_scheme:
                selected_scheme = scheme_name
                self.tags['option_list'].add_option((None, ''), _("{item_name} (Selected)").format(item_name=scheme_name))
            else:
                self.tags['option_list'].add_option(('select-scheme', scheme_name), scheme_name)

        self.tags['option_list'].add_option(None, "")
        self.tags['option_list'].add_option(('back', None), _("Back"))
        self.set_buttons({'A': _('Select'), 'B': _('Back')})

    def do_update(self, events):
        super().do_update(events)

        if events.was_pressed('A'):
            selected_option, selected_parameter = self.tags['option_list'].selected_option()

            self.button_activate()

            # print(f"Selected {selected_option} -> {selected_parameter}")

            if selected_option == 'back':
                self.gui.pop_scene()
                return True

            elif selected_option == 'select-scheme':
                if self.gui.message_box(_("Do you want to change the themes color scheme?\n\nYou will have to restart for it to take affect."), want_cancel=True):
                    self.gui.hm.cfg_data['theme-scheme'] = selected_parameter
                    self.gui.hm.save_config()
                    self.gui.events.running = False

                    if not harbourmaster.HM_TESTING:
                        reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                        if not reboot_file.is_file():
                            reboot_file.touch(0o644)

                    return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class LanguageScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Language Select")

        self.load_regions("option_menu", ['option_list', ])

        languages = gui.lang_list
        selected_lang = os.environ['LANG']

        self.tags['option_list'].reset_options()
        for lang_code, lang_name in languages.items():
            if lang_code == selected_lang:
                self.tags['option_list'].add_option((None, ''), _("{lang_name} (Selected)").format(lang_name=lang_name))
            else:
                self.tags['option_list'].add_option(('select-language', lang_code), lang_name)

        self.tags['option_list'].add_option(None, "")
        self.tags['option_list'].add_option(('back', None), _("Back"))
        self.set_buttons({'A': _('Select'), 'B': _('Back')})

    def do_update(self, events):
        super().do_update(events)

        if events.was_pressed('A'):
            selected_option, selected_parameter = self.tags['option_list'].selected_option()

            self.button_activate()

            # print(f"Selected {selected_option} -> {selected_parameter}")

            if selected_option == 'back':
                self.gui.pop_scene()
                return True

            elif selected_option == 'select-language':
                if self.gui.message_box(_("Do you want to change language?\n\nYou will have to restart for it to take affect."), want_cancel=True):
                    if selected_parameter == DEFAULT_LANG:
                        del self.gui.hm.cfg_data['language']

                    else:
                        self.gui.hm.cfg_data['language'] = selected_parameter

                    self.gui.hm.save_config()
                    self.gui.events.running = False

                    if not harbourmaster.HM_TESTING:
                        reboot_file = (harbourmaster.HM_TOOLS_DIR / "PortMaster" / ".pugwash-reboot")
                        if not reboot_file.is_file():
                            reboot_file.touch(0o644)

                    return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class OnScreenKeyboard(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)

        self.load_regions("on_screen_keyboard", ['keyboard'])

        self.mode = 'lower'
        self.build_keyboard()
        self.tags['keyboard'].list_select(2)
        self.tags['keyboard'].bar_select(4, 2)

    def build_keyboard(self, keep=False):
        if keep:
            last_list_select = self.tags['keyboard'].list_selected()
            last_bar_select = self.tags['keyboard'].bar_selected()

        self.tags['keyboard'].reset_options()
        if self.mode == 'lower':
            self.tags['keyboard'].add_option('row-1', [' ` ', ' 1 ', ' 2 ', ' 3 ', ' 4 ', ' 5 ', ' 6 ', ' 7 ', ' 8 ', ' 9 ', ' 0 ', ' - ', ' = '], 0)
            self.tags['keyboard'].add_option('row-2', [' q ', ' w ', ' e ', ' r ', ' t ', ' y ', ' u ','  i ', ' o ', ' p ', ' [ ', ' ] ', ' \\ '], 0)
            self.tags['keyboard'].add_option('row-3', [' a ', ' s ', ' d ', ' f ', ' g ', ' h ', ' j ', ' k ', ' l ', ' ; ', ' \' '], 0)
            self.tags['keyboard'].add_option('row-4', [' z ', ' x ', ' c ', ' v ', ' b ', ' n ', ' m ', ' , ', ' . ', ' / '], 0)
            self.tags['keyboard'].add_option('row-5', [' UPPER ', '    [_____]    ', ' << '], 0)

            self.set_buttons({'A': _('Select'), 'B': _('Delete'), 'X': _('Upper Case'), 'Y': _('Space'), 'START': _('Enter'), 'SELECT': _('Cancel')})
        else:
            self.tags['keyboard'].add_option('row-1', [' ~ ', ' ! ', ' @ ', ' # ', ' $ ', ' % ', ' ^ ', ' & ', ' * ', ' ( ', ' ) ', ' _ ', ' + '], 0)
            self.tags['keyboard'].add_option('row-2', [' Q ', ' W ', ' E ', ' R ', ' T ', ' Y ', ' U ','  I ', ' O ', ' P ', ' { ', ' } ', ' | '], 0)
            self.tags['keyboard'].add_option('row-3', [' A ', ' S ', ' D ', ' F ', ' G ', ' H ', ' J ', ' K ', ' L ', ' : ', ' " '], 0)
            self.tags['keyboard'].add_option('row-4', [' Z ', ' X ', ' C ', ' V ', ' B ', ' N ', ' M ', ' < ', ' > ', ' ? '], 0)
            self.tags['keyboard'].add_option('row-5', [' LOWER ', '    [_____]    ', ' << '], 0)

            self.set_buttons({'A': _('Select'), 'B': _('Delete'), 'X': _('Lower Case'), 'Y': _('Space'), 'START': _('Enter'), 'SELECT': _('Cancel')})

        if keep:
            self.tags['keyboard'].list_select(last_list_select)
            self.tags['keyboard'].bar_select(last_bar_select)

        self.last_select = self.tags['keyboard'].list_selected()

    def do_update(self, events):
        super().do_update(events)

        if self.last_select != self.tags['keyboard'].list_selected():
            item = self.tags['keyboard'].bar_selected(self.last_select)
            self.tags['keyboard'].bar_select(item)
            self.last_select = self.tags['keyboard'].list_selected()

        if events.was_pressed('START') or events.was_pressed('A'):
            selected_option = self.tags['keyboard'].selected_option()
            selected_key = self.tags['keyboard'].bar_selected()

            # print(_("Selected {selected_option}"))

            if selected_option == 'back':
                self.gui.pop_scene()
                return True

            return True

        elif events.was_pressed('B'):
            self.gui.pop_scene()
            return True

        elif events.was_pressed('X'):
            if self.mode == 'upper':
                self.mode = 'lower'
            else:
                self.mode = 'upper'

            self.build_keyboard(keep=True)


class FeaturedPortsListScene(BaseScene):
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Featured Ports")

        self.load_regions("featured_ports_list", ['option_list', ])

        self.featured_ports = self.gui.hm.featured_ports(pre_load=True)

        self.tags['option_list'].reset_options()

        for idx, port_list in enumerate(self.featured_ports):
            self.tags['option_list'].add_option(idx, port_list['name'])

        self.set_buttons({'A': _('Select'), 'B': _('Back')})
        self.update_selection()

    def update_selection(self):
        if len(self.featured_ports) == 0:
            return

        selected = self.tags['option_list'].selected_option()
        port_list = self.featured_ports[selected]
        self.gui.set_data('featured_ports.name', port_list['name'])
        self.gui.set_data('featured_ports.description', port_list['description'])
        self.gui.set_data('featured_ports.image', str(port_list['image']))
        self.last_select = selected

    def do_update(self, events):
        super().do_update(events)
        if len(self.featured_ports) == 0:
            self.gui.message_box(_("No featured ports found, internet connection is required."))
            self.gui.pop_scene()
            return True

        selected = self.tags['option_list'].selected_option()

        if self.last_select != selected:
            self.update_selection()

        if events.was_pressed('A'):
            self.button_activate()
            self.gui.push_scene('port-list', FeaturedPortsScene(self.gui, self.featured_ports[selected]))
            return True

        elif events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True


class PortListBaseScene():
    def update_ports(self):
        if self.gui.hm is None:
            self.all_ports = {}
            self.port_list = []
            self.last_port = 0
            self.tags['ports_list'].selected = 0
            self.tags['ports_list'].list = [
                _('NO PORTS')]

            self.gui.set_port_info(None, {})

            self.gui.set_data("port_info.title", _("** NO PORTS FOUND **"))

            self.gui.set_data("port_info.description", _("Download ports first."))
            self.ready = True
            return

        if not self.ready:
            self.gui.set_data('ports_list.total_ports', str(len(self.gui.hm.list_ports(filters=(self.options['base_filters'])))))

        self.all_ports = self.gui.hm.list_ports(
            filters=(self.options['base_filters'] + self.options['filters']),
            sort_by=self.options['sort_by'])
        self.port_list = list(self.all_ports.keys())

        self.gui.set_data('ports_list.filters', ', '.join(sorted(self.options['filters'])))
        self.gui.set_data('ports_list.filter_ports', str(len(self.port_list)))

        if len(self.port_list) == 0:
            self.tags['ports_list'].list = [
                _('NO PORTS')]

            # if 'port_image' in self.tags:
            #     self.tags['port_image'].image = self.gui.get_port_image("no-image")

            self.gui.set_port_info(None, {})

            self.gui.set_data("port_info.title", _("** NO PORTS FOUND **"))

            if len(self.options['filters']) == 0:
                self.gui.set_data("port_info.description", _("Download ports first."))
            else:
                self.gui.set_data("port_info.description", _("Try removing some filters."))

        else:
            self.tags['ports_list'].list = [
                self.all_ports[port_name]['attr']['title']
                for port_name in self.port_list]

        if self.tags['ports_list'].selected >= len(self.port_list):
            if len(self.port_list) == 0:
                self.tags['ports_list'].selected = 0

            else:
                self.tags['ports_list'].selected = len(self.port_list) - 1

        self.last_port = self.tags['ports_list'].selected + 1
        self.ready = True

    def try_to_select(self, port_name, port_title):
        ## Try and select a port
        if port_name in self.port_list:
            # We found it
            self.tags['ports_list'].selected = self.port_list.index(port_name)
            self.last_port = self.tags['ports_list'].selected + 1
            return

        ## Okay find a port with a name greater than ours, and then select the one before it.
        for i in range(len(self.port_list)):
            if self.all_ports[self.port_list[i]]['attr']['title'] > port_title:
                self.tags['ports_list'].selected = max(i-1, 0)
                self.last_port = self.tags['ports_list'].selected + 1
                return

        ## Do nothing.

    def selected_port(self):
        if len(self.port_list) == 0:
            return 0

        self.last_port = self.tags['ports_list'].selected
        return self.port_list[self.last_port]

    def select_next_port(self):
        if len(self.port_list) == 0:
            return

        self.last_port = self.tags['ports_list'].selected = (self.last_port + 1) % len(self.port_list)

        port_name = self.port_list[self.last_port]
        port_info = self.all_ports[port_name]

        self.gui.set_port_info(port_name, port_info, self.options['mode'] != 'install')

        return port_name

    def select_prev_port(self):
        if len(self.port_list) == 0:
            return

        self.last_port = self.tags['ports_list'].selected = (self.last_port - 1) % len(self.port_list)

        port_name = self.port_list[self.last_port]
        port_info = self.all_ports[port_name]

        self.gui.set_port_info(port_name, port_info, self.options['mode'] != 'install')

        return port_name

    def do_update(self, events):
        super().do_update(events)
        if not self.ready:
            self.update_ports()
            if not self.ready:
                return True

        if len(self.port_list) > 0 and self.last_port != self.tags['ports_list'].selected:
            self.last_port = self.tags['ports_list'].selected

            port_name = self.port_list[self.last_port]
            port_info = self.all_ports[port_name]

            self.gui.set_port_info(port_name, port_info, self.options['mode'] != 'install')
            # print(json.dumps(port_info, indent=4))

            # if 'port_image' in self.tags:
            #     self.tags['port_image'].image = self.gui.get_port_image(port_name)

        if self.options['mode'] in ('install', 'uninstall') and events.was_pressed('X'):
            self.button_activate()

            if len(self.port_list) > 0 or len(self.options['filters']) > 0:
                self.gui.push_scene('ports', FiltersScene(self.gui, self))

            return True

        if events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True

        if events.was_pressed('A') and len(self.port_list) > 0:
            self.button_activate()
            self.last_port = self.tags['ports_list'].selected
            port_name = self.port_list[self.last_port]

            logger.debug(f"{self.options['mode']}: {port_name}")
            if self.options['mode'] == 'featured-ports':
                # self.ready = False
                self.gui.push_scene('port_info', PortInfoScene(self.gui, port_name, 'install', self))

            elif self.options['mode'] == 'install':
                self.ready = False
                self.gui.push_scene('port_info', PortInfoScene(self.gui, port_name, 'install', self))

            elif self.options['mode'] == 'uninstall':
                self.ready = False
                self.gui.push_scene('port_info', PortInfoScene(self.gui, port_name, 'uninstall', self))

            return True


class FeaturedPortsScene(PortListBaseScene, BaseScene):
    def __init__(self, gui, options):
        super().__init__(gui)
        self.scene_title = options['name']

        self.load_regions("featured_ports", [
            'ports_list',
            ])

        self.options = {
            'mode': 'featured-ports',
            'filters': [],
            'base_filters': [],
            'skip_genres': [],
            }

        self.options.update(**options)

        self.all_ports = options['ports']
        self.port_list = list(options['ports'].keys())

        self.gui.set_data('featured_ports.name', options['name'])
        self.gui.set_data('featured_ports.description', options['description'])
        self.gui.set_data('featured_ports.image', str(options['image']))
        self.gui.set_data('ports_list.total_ports', str(len(self.port_list)))
        self.gui.set_data('ports_list.filter_ports', str(len(self.port_list)))
        self.gui.set_data('ports_list.filters', "")

        self.ready = True
        self.tags['ports_list'].list = [
            self.all_ports[port_name]['attr']['title']
            for port_name in self.port_list]

        self.last_port = self.tags['ports_list'].selected + 1

        self.set_buttons({'A': _('Show Info'), 'B': _('Back')})

class PortsListScene(PortListBaseScene, BaseScene):
    def __init__(self, gui, options):
        super().__init__(gui)
        self.scene_title = _("Ports List")

        self.options = options
        self.options.setdefault('base_filters', [])
        self.options.setdefault('filters', [])
        self.options.setdefault('sort_by', 'alphabetical')
        self.options.setdefault('skip_genres', [])

        if self.options['mode'] == 'install':
            self.options['skip_genres'].append('broken')

        self.load_regions("ports_list", [
            'ports_list',
            ])

        self.ready = False
        self.update_ports()

        if self.options['mode'] == 'install':
            self.set_buttons({'A': _('Show Info'), 'B': _('Back'), 'X': _('Filters')})
        else:
            self.set_buttons({'A': _('Show Info'), 'B': _('Back')})


class PortInfoPopup(BaseScene):
    def __init__(self, gui, parent):
        super().__init__(gui)
        self.scene_title = _("Port Info Popup")
        self.parent_info_scene = parent
        self.load_regions("port_info_popup", [])
        self.update_selection()

    def update_selection(self):
        buttons = {}

        if 'buttons' in self.config:
            for button, action in self.config['buttons'].items():
                if action == 'pop_scene':
                    buttons[button] = _('Hide Info')

        else:
            buttons['DOWN'] = _('Hide Info')

            self.config['buttons'] = {
                'DOWN': 'pop_scene',
                }

        if 'installed' in self.parent_info_scene.port_attrs:
            buttons.update({'A': _('Reinstall'), 'Y': _('Uninstall'), 'B': _('Back')})

        else:
            buttons.update({'A': _('Install'), 'B': _('Back')})

        self.port_name = self.gui.get_data("port_info.name")
        self.set_buttons(buttons)

    def do_update(self, events):
        super().do_update(events)

        if self.port_name != self.gui.get_data("port_info.name"):
            self.update_selection()

        for action in self.config_buttons(events):
            if action == "pop_scene":
                self.parent_info_scene.popup_shown = False
                self.button_back()
                self.gui.pop_scene()
                return True

        return False


class PortInfoScene(BaseScene):
    def __init__(self, gui, port_name, action, port_list_scene):
        super().__init__(gui)
        self.scene_title = _("Port Info")

        self.load_regions("port_info", [])

        self.port_name = port_name
        self.port_list_scene = port_list_scene
        self.popup_shown = False
        self.action = action
        self.ready = False
        self.update_port()

    def update_port(self):
        if self.gui.hm is None:
            return

        self.port_info = self.gui.hm.port_info(self.port_name, installed=(self.action != 'install'))

        self.port_attrs = self.gui.hm.port_info_attrs(self.port_info)

        logger.debug(f"{self.action}: {self.port_name} -> {self.port_attrs} -> {self.port_info}")

        # if 'port_image' in self.tags:
        #     self.tags['port_image'].image = self.gui.get_port_image(self.port_name)

        self.gui.set_port_info(self.port_name, self.port_info)

        buttons = {}

        if 'buttons' in self.config:
            for button, action in self.config['buttons'].items():
                if action == 'port_info_popup' and 'port_info_popup' in self.gui.theme_data:
                    buttons[button] = _('Show Info')

        else:

            if 'port_info_popup' in self.gui.theme_data:
                buttons['UP'] = _('Show Info')

                self.config['buttons'] = {
                    'UP': 'port_info_popup',
                    'LEFT': 'prev_port',
                    'RIGHT': 'next_port',
                    }

            else:
                self.config['buttons'] = {
                    'UP': 'prev_port',
                    'DOWN': 'next_port',
                    }

        if 'installed' in self.port_attrs:
            buttons.update({'A': _('Reinstall'), 'Y': _('Uninstall'), 'B': _('Back')})
        else:
            buttons.update({'A': _('Install'), 'B': _('Back')})

        self.set_buttons(buttons)

        self.ready = True

    def do_update(self, events):
        super().do_update(events)

        if events.was_pressed('A'):
            self.button_activate()
            self.gui.pop_scene()

            # if self.action == 'install':
            self.gui.do_install(self.port_name)

            return True

        if events.was_pressed('Y'):
            if 'installed' in self.port_attrs:
                if self.gui.message_box(_("Are you sure you want to uninstall {port_name}?").format(
                        port_name=self.port_info['attr']['title']), want_cancel=True):

                    self.gui.do_uninstall(self.port_name)
                    self.gui.pop_scene('port_info')

        if events.was_pressed('B'):
            self.button_back()
            self.gui.pop_scene()
            return True

        for action in self.config_buttons(events):
            if action == "port_info_popup":
                if 'port_info_popup' in self.gui.theme_data:
                    if self.popup_shown:
                        return True

                    scene = PortInfoPopup(self.gui, self)
                    scene.button_activate()
                    self.popup_shown = True
                    self.gui.push_scene('port_info', scene)
                    return True

            if action == "prev_port":
                self.port_name = self.port_list_scene.select_prev_port()
                self.update_port()
                self.button_activate()
                return True

            if action == "next_port":
                self.port_name = self.port_list_scene.select_next_port()
                self.update_port()
                self.button_activate()
                return True

            else:
                logger.debug(f"Unknown #config.button action: {action}")

        return False


class FiltersScene(BaseScene):
    def __init__(self, gui, list_scene):
        super().__init__(gui)
        self.scene_title = _("Filters Scene")

        self.load_regions("filter_list", [
            'filter_list',
            ])

        self.list_scene = list_scene
        self.locked_genres = list(list_scene.options['base_filters'])
        self.selected_genres = list(list_scene.options['filters'])
        self.sort_by = list_scene.options.get('sort_by', "alphabetical")
        self.selected_port = list_scene.selected_port()

        if len(list_scene.all_ports) > 0:
            self.selected_port_title = list_scene.all_ports[self.selected_port]['attr']['title']
        else:
            ## Christian_Hatian wins again!
            self.selected_port_title = "2048.zip"

        self.port_list = []

        self.ready = False
        self.update_filters()

    def update_filters(self):
        if self.gui.hm is None:
            return

        filter_translation = {
            # Sorting.
            "alphabetical":     _("Alphabetical"),
            "recently_added":   _("Recently Added"),
            "recently_updated": _("Recently Updated"),

            # Genres.
            "action":           _("Action"),
            "adventure":        _("Adventure"),
            "arcade":           _("Arcade"),
            "casino/card":      _("Casino/Card"),
            "fps":              _("First Person Shooter"),
            "platformer":       _("Platformer"),
            "puzzle":           _("Puzzle"),
            "racing":           _("Racing"),
            "rhythm":           _("Rhythm"),
            "rpg":              _("Role Playing Game"),
            "simulation":       _("Simulation"),
            "sports":           _("Sports"),
            "strategy":         _("Strategy"),
            "visual novel":     _("Visual Novel"),
            "other":            _("Other"),

            # Attrs.
            "rtr":              _("Ready to Run"),
            "not installed":    _("Not Installed"),
            "update available": _("Update Available"),
            "broken":           _("Broken Ports"),

            # Availability.
            "full":             _("Free game, all files included."),
            "demo":             _("Demo files included."),
            "free":             _("Free external assets needed."),
            "paid":             _("Paid external assets needed."),

            # Runtimes.
            "mono":             _("{runtime_name} Runtime").format(runtime_name="Mono"),
            "godot":            _("{runtime_name} Runtime").format(runtime_name="Godot/FRT"),
            }

        # Hack to make other appear last, by default the order will be 0, you can set it to -1 for it to appear at the top.
        sort_order = {
            'other': 1,
            }

        genres = self.locked_genres + self.selected_genres
        total_ports = len(self.gui.hm.list_ports(genres))

        self.tags['filter_list'].bar_select_mode = 'full'

        first_add = True
        add_blank = False

        selected_option = self.tags['filter_list'].selected_option()
        selected_offset = 0

        self.tags['filter_list'].reset_options()

        DISPLAY_ORDER = [
            'sort',
            'clear-filters',
            'attr',
            # 'status',
            'genres',
            'porters',
            ]

        for display_order in DISPLAY_ORDER:
            first_add = True

            if display_order == 'sort':
                for hm_sort_order in harbourmaster.HM_SORT_ORDER:
                    if hm_sort_order == self.sort_by:
                        text = ["    ", "_CHECKED", f"  {filter_translation.get(hm_sort_order, hm_sort_order)}", None, "    "]
                    else:
                        text = ["    ", "_UNCHECKED", f"  {filter_translation.get(hm_sort_order, hm_sort_order)}", None, "    "]

                    if first_add:
                        if add_blank:
                            self.tags['filter_list'].add_option(None, "")

                        self.tags['filter_list'].add_option(None, _("Sort:"))
                        first_add = False
                        add_blank = True

                    self.tags['filter_list'].add_option(hm_sort_order, text)

                    if selected_option == hm_sort_order:
                        selected_offset = len(self.tags['filter_list'].options) - 1

            elif display_order == 'clear-filters':
                if len(self.selected_genres) > 0:
                    if add_blank:
                        self.tags['filter_list'].add_option(None, "")

                    add_blank = True
                    self.tags['filter_list'].add_option('clear-filters', ["     ", _("Clear Filters"), "    "])

            elif display_order == 'genres':
                for hm_genre in sorted(harbourmaster.HM_GENRES, key=lambda genre: (sort_order.get(genre, 0), filter_translation.get(genre, genre))):
                    if hm_genre in self.locked_genres:
                        continue

                    if hm_genre in self.list_scene.options['skip_genres']:
                        continue

                    if hm_genre in genres:
                        ports = total_ports
                        text = ["    ", "_CHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports} "]
                    else:
                        ports = len(self.gui.hm.list_ports(genres + [hm_genre]))
                        text = ["    ", "_UNCHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports} "]

                    if ports == 0:
                        continue

                    if first_add:
                        if add_blank:
                            self.tags['filter_list'].add_option(None, "")

                        self.tags['filter_list'].add_option(None, _("Genres:"))
                        first_add = False
                        add_blank = True

                    self.tags['filter_list'].add_option(hm_genre, text)

                    if selected_option == hm_genre:
                        selected_offset = len(self.tags['filter_list'].options) - 1

            elif display_order == 'attr':
                for hm_genre in ['rtr', 'mono', 'godot', 'not installed', 'update available', 'broken']:
                    if hm_genre in self.locked_genres:
                        continue

                    if hm_genre in self.list_scene.options['skip_genres']:
                        continue

                    if hm_genre in genres:
                        ports = total_ports
                        text = ["    ", "_CHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]
                    else:
                        ports = len(self.gui.hm.list_ports(genres + [hm_genre]))
                        text = ["    ", "_UNCHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]

                    if ports == 0:
                        continue

                    if first_add:
                        if add_blank:
                            self.tags['filter_list'].add_option(None, "")
                        self.tags['filter_list'].add_option(None, _("Attributes:"))
                        first_add = False

                    self.tags['filter_list'].add_option(hm_genre, text)

                    if selected_option == hm_genre:
                        selected_offset = len(self.tags['filter_list'].options) - 1

            elif display_order == 'status':
                for hm_genre in ['full', 'demo', 'free', 'paid']:
                    if hm_genre in self.locked_genres:
                        continue

                    if hm_genre in self.list_scene.options['skip_genres']:
                        continue

                    if hm_genre in genres:
                        ports = total_ports
                        text = ["    ", "_CHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]
                    else:
                        ports = len(self.gui.hm.list_ports(genres + [hm_genre]))
                        text = ["    ", "_UNCHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]

                    if ports == 0:
                        continue

                    if first_add:
                        if add_blank:
                            self.tags['filter_list'].add_option(None, "")
                        self.tags['filter_list'].add_option(None, _("Availability:"))
                        first_add = False

                    self.tags['filter_list'].add_option(hm_genre, text)

                    if selected_option == hm_genre:
                        selected_offset = len(self.tags['filter_list'].options) - 1

            elif display_order == 'porters':
                for hm_genre in sorted(self.gui.hm.porters_list(), key=lambda name: name.lower()):
                    if hm_genre in self.locked_genres:
                        continue

                    if hm_genre in self.list_scene.options['skip_genres']:
                        continue

                    if hm_genre in genres:
                        ports = total_ports
                        text = ["    ", "_CHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]
                    else:
                        ports = len(self.gui.hm.list_ports(genres + [hm_genre]))
                        text = ["    ", "_UNCHECKED", f"  {filter_translation.get(hm_genre, hm_genre)}", None, "    ", f"  {ports}"]

                    if ports == 0:
                        continue

                    if first_add:
                        if add_blank:
                            self.tags['filter_list'].add_option(None, "")
                        self.tags['filter_list'].add_option(None, _("Porters:"))
                        first_add = False

                    self.tags['filter_list'].add_option(hm_genre, text)

                    if selected_option == hm_genre:
                        selected_offset = len(self.tags['filter_list'].options) - 1

        self.tags['filter_list'].list_select(selected_offset, direction=1)

        self.ready = True

    def do_update(self, events):
        super().do_update(events)
        if not self.ready:
            self.update_filters()
            if not self.ready:
                return True

        if events.was_pressed('A'):
            selected_filter = self.tags['filter_list'].options[self.tags['filter_list'].selected]
            if selected_filter is None:
                return True

            if selected_filter in harbourmaster.HM_SORT_ORDER:
                if self.sort_by == selected_filter:
                    return True

                self.sort_by = selected_filter
                self.list_scene.options['sort_by'] = selected_filter
                self.list_scene.update_ports()
                self.list_scene.tags['ports_list'].list_select(0)
                self.update_filters()
                self.button_activate()
                return True

            if selected_filter == 'clear-filters':
                self.selected_genres.clear()

            elif selected_filter in self.selected_genres:
                self.selected_genres.remove(selected_filter)

            else:
                self.selected_genres.append(selected_filter)

            self.update_filters()
            self.list_scene.options['filters'] = self.selected_genres
            self.list_scene.update_ports()
            self.list_scene.try_to_select(self.selected_port, self.selected_port_title)
            self.button_activate()
            return True

        if events.was_pressed('B') or events.was_pressed('X'):
            self.button_back()
            self.gui.pop_scene()
            return True

        return True


class MessageWindowScene(BaseScene):
    """
    This is a scrolling window showing messages for downloading/installing/uninstalling/updating.

    It can have an optional progress bar at the bottom.
    """
    def __init__(self, gui):
        super().__init__(gui)
        self.scene_title = _("Messages")

        self.load_regions("message_window", [
            'message_text'
            ])

        self.cancellable = not self.gui.cancellable
        self.update_buttons()

    def update_buttons(self):
        if self.cancellable == self.gui.cancellable:
            return

        self.cancellable = self.gui.cancellable

        if self.cancellable:
            self.set_buttons({'B': _('Cancel')})
        else:
            self.set_buttons({})

    def do_update(self, events):
        super().do_update(events)
        # sdl2.SDL_Delay(1000)

        self.update_buttons()

        if 'progress_bar' in self.tags:
            if self.gui.callback_amount is not None:
                self.tags['progress_bar'].progress_amount = self.gui.callback_amount
            else:
                self.tags['progress_bar'].progress_amount = 0

        if events.was_pressed('B'):
            if self.gui.cancellable:
                if self.gui.message_box(
                        _('Are you sure you want to cancel?'),
                        want_cancel=True):
                    self.gui.do_cancel()


class MessageBoxScene(BaseScene):
    def __init__(self, gui, message, *, title_text=None, want_cancel=False, ok_text=None, cancel_text=None):
        super().__init__(gui)
        if title_text is not None:
            self.scene_title = _(title_text)

        if ok_text is None:
            ok_text = _("Okay")

        if cancel_text is None:
            cancel_text = _("Cancel")

        self.load_regions("message_box", ['message_text', ])

        self.tags['message_text'].text = message

        buttons = {}
        if want_cancel:
            self.set_buttons({'A': ok_text, 'B': cancel_text})

        else:
            self.set_buttons({'A': ok_text})


class DialogSelectionList(BaseScene):
    def __init__(self, gui, options, register):
        super().__init__(gui)

        self.scene_title = options.get('title', "")

        self.options = options
        self.register = register

        self.gui.set_data("selection_list.title", "")
        self.gui.set_data("selection_list.description", "")
        self.gui.set_data("selection_list.image", "NO_IMAGE")

        scene = ("selection_list"
                + (options.get('want_description', False) and "_description" or "")
                + (options.get('want_images', False) and "_images" or "")
                )

        self.load_regions(scene, [
            'selection_list',
            ])

        self.tags['selection_list'].reset_options()

        for reg_key, reg_values in register.items():
            self.tags['selection_list'].add_option(reg_key, reg_values.get("title", reg_key))

        self.last_selection = None
        self.update_selection()

        if self.options.get('want_cancel', False):
            self.set_buttons({'A': _("Okay"), 'B': _("Cancel")})
        else:
            self.set_buttons({'A': _("Okay")})

    def update_selection(self):
        selection = self.tags['selection_list'].selected_option()

        if selection == None:
            self.gui.set_data("selection_list.title", "")
            self.gui.set_data("selection_list.description", "")
            self.gui.set_data("selection_list.image", "NO_IMAGE")

        else:
            self.gui.set_data("selection_list.title", self.register[selection].get("title", ""))
            self.gui.set_data("selection_list.description", self.register[selection].get("description", ""))
            self.gui.set_data("selection_list.image", self.register[selection].get("image", "NO_IMAGE"))

        self.last_selection = selection

    def selected_option(self):
        return self.tags['selection_list'].selected_option()

    def do_update(self, events):
        super().do_update(events)

        if self.last_selection != self.tags['selection_list'].selected_option():
            self.update_selection()

        return True


__all__ = (
    'StringFormatter',
    'BaseScene',
    'BlankScene',
    'DialogSelectionList',
    'FiltersScene',
    'LanguageScene',
    'MainMenuScene',
    'MessageBoxScene',
    'MessageWindowScene',
    'OnScreenKeyboard',
    'OptionScene',
    'PortInfoScene',
    'PortsListScene',
    'RuntimesScene',
    'SourceScene',
    'ThemeSchemeScene',
    'ThemesScene',
    )
