# -*- Mode: Python; coding: utf-8; indent-tabs-mode: nil; tab-width: Bind a new key
import os
import logging
import json

from gi.repository import Gio, Gtk, WebKit2, GLib
from urllib import unquote
from base64 import b64decode

from ulauncher.api.shared.action.OpenUrlAction import OpenUrlAction
from ulauncher.config import get_data_file, get_options, get_version, EXTENSIONS_DIR
from ulauncher.search.shortcuts.ShortcutsDb import ShortcutsDb
from ulauncher.ui.AppIndicator import AppIndicator
from ulauncher.util.AutostartPreference import AutostartPreference
from ulauncher.util.Router import Router, get_url_params
from ulauncher.util.Settings import Settings
from ulauncher.util.string import force_unicode
from ulauncher.util.decorator.run_async import run_async
from ulauncher.util.decorator.glib_idle_add import glib_idle_add
from ulauncher.api.server.ExtensionServer import ExtensionServer
from ulauncher.api.server.ExtensionDownloader import (ExtensionDownloader, ExtensionDownloaderError,
                                                      ExtensionIsUpToDateError)
from ulauncher.api.server.ExtensionManifest import ManifestValidationError, VersionIncompatibilityError
from ulauncher.api.server.ExtensionRunner import ExtensionRunner
from ulauncher.api.server.ExtensionDb import ExtensionDb
from ulauncher.api.server.ExtensionPreferences import ExtensionPreferences
from ulauncher.api.server.extension_finder import find_extensions
from ulauncher.api.shared.event import PreferencesUpdateEvent
from .Builder import Builder
from .WindowHelper import WindowHelper
from .HotkeyDialog import HotkeyDialog


logger = logging.getLogger(__name__)
rt = Router()


class PrefsApiError(RuntimeError):
    pass


class PreferencesUlauncherDialog(Gtk.Dialog, WindowHelper):
    __gtype_name__ = "PreferencesUlauncherDialog"

    _hotkey_name = None

    ######################################
    # Initialization
    ######################################

    def __new__(cls):
        """Special static method that's automatically called by Python when
        constructing a new instance of this class.

        Returns a fully instantiated PreferencesDialog object.
        """
        builder = Builder.new_from_file('PreferencesUlauncherDialog')
        new_object = builder.get_object("preferences_ulauncher_dialog")
        new_object.finish_initializing(builder)
        return new_object

    def finish_initializing(self, builder):
        """Called while initializing this instance in __new__

        finish_initalizing should be called after parsing the ui definition
        and creating a PreferencesDialog object with it in order to
        finish initializing the start of the new PerferencesUlauncherDialog
        instance.

        Put your initialization code in here and leave __init__ undefined.
        """

        # Get a reference to the builder and set up the signals.
        self.builder = builder
        self.ui = builder.get_ui(self, True)

        # unnecessary action area can be removed only manually, like this
        self.ui['dialog_action_area'].destroy()

        self.settings = Settings.get_instance()
        self._init_webview()
        self.autostart_pref = AutostartPreference()
        self.hotkey_dialog = HotkeyDialog()
        self.hotkey_dialog.connect('hotkey-set', self.on_hotkey_set)

        self.show_all()

    def _init_webview(self):
        """
        Initialize preferences WebView
        """
        self.webview = WebKit2.WebView()
        self.ui['scrolled_window'].add(self.webview)
        opts = get_options()
        self._load_prefs_html()

        web_settings = self.webview.get_settings()
        web_settings.set_enable_developer_extras(bool(opts.dev))
        web_settings.set_enable_xss_auditor(False)
        web_settings.set_enable_write_console_messages_to_stdout(True)

        self.webview.get_context().register_uri_scheme('prefs', self.on_scheme_callback)
        self.webview.get_context().set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)  # disable caching
        self.webview.connect('context-menu', self.webview_on_context_menu)

        inspector = self.webview.get_inspector()
        inspector.connect("attach", lambda inspector, target_view: WebKit2.WebView())

    ######################################
    # Overrides
    ######################################

    def present(self, page):
        self._load_prefs_html(page)
        super(PreferencesUlauncherDialog, self).present()

    def show(self, page):
        self._load_prefs_html(page)
        super(PreferencesUlauncherDialog, self).show()

    ######################################
    # GTK event handlers
    ######################################

    def webview_on_context_menu(self, *args):
        return bool(not get_options().dev)

    ######################################
    # WebView communication methods
    ######################################

    @run_async
    def on_scheme_callback(self, scheme_request):
        """
        Handles Javascript-to-Python calls
        """

        try:
            params = get_url_params(scheme_request.get_uri())
            callback_name = params['query']['callback']
            assert callback_name
        except Exception as e:
            logger.exception('API call failed. %s: %s' % (type(e).__name__, e.message))
            return

        try:
            resp = rt.dispatch(self, scheme_request.get_uri())
            callback = '%s(%s);' % (callback_name, json.dumps(resp))
        except PrefsApiError as e:
            callback = '%s(null, %s);' % (callback_name, json.dumps(e.message))
        except Exception as e:
            message = 'Unexpected API error. %s: %s' % (type(e).__name__, e.message)
            callback = '%s(null, %s);' % (callback_name, json.dumps(message))
            logger.exception(message)

        try:
            stream = Gio.MemoryInputStream.new_from_data(callback)
            # send response
            scheme_request.finish(stream, -1, 'text/javascript')
        except Exception as e:
            logger.exception('Unexpected API error. %s: %s' % (type(e).__name__, e.message))

    def send_webview_notification(self, name, data):
        self.webview.run_javascript('onNotification("%s", %s)' % (name, json.dumps(data)))

    ######################################
    # Request handlers
    ######################################

    @rt.route('/get/all')
    def prefs_get_all(self, url_params):
        logger.info('API call /get/all')
        return {
            'show_indicator_icon': self.settings.get_property('show-indicator-icon'),
            'hotkey_show_app': self.get_app_hotkey(),
            'autostart_allowed': self.autostart_pref.is_allowed(),
            'autostart_enabled': self.autostart_pref.is_on(),
            'show_recent_apps': self.settings.get_property('show-recent-apps'),
            'clear_previous_query': self.settings.get_property('clear-previous-query'),
            'blacklisted_desktop_dirs': self.settings.get_property('blacklisted-desktop-dirs'),
            'theme_name': self.settings.get_property('theme-name'),
            'env': {
                'version': get_version(),
                'user_home': os.path.expanduser('~')
            }
        }

    @rt.route('/set/show-indicator-icon')
    def prefs_set_show_indicator_icon(self, url_params):
        show_indicator = self._get_bool(url_params['query']['value'])
        logger.info('Set show-indicator-icon to %s' % show_indicator)
        self.settings.set_property('show-indicator-icon', show_indicator)
        self.settings.save_to_file()
        indicator = AppIndicator.get_instance()
        GLib.idle_add(indicator.switch, show_indicator)

    @rt.route('/set/autostart-enabled')
    def prefs_set_autostart(self, url_params):
        is_on = self._get_bool(url_params['query']['value'])
        logger.info('Set autostart-enabled to %s' % is_on)
        if is_on and not self.autostart_pref.is_allowed():
            raise PrefsApiError("Unable to turn on autostart preference")

        try:
            self.autostart_pref.switch(is_on)
        except Exception as e:
            raise PrefsApiError('Caught an error while switching "autostart": %s' % e)

    @rt.route('/set/show-recent-apps')
    def prefs_set_show_recent_apps(self, url_params):
        is_on = self._get_bool(url_params['query']['value'])
        logger.info('Set show-recent-apps to %s' % is_on)
        self.settings.set_property('show-recent-apps', is_on)
        self.settings.save_to_file()

    @rt.route('/set/hotkey-show-app')
    @glib_idle_add
    def prefs_set_hotkey_show_app(self, url_params):
        hotkey = url_params['query']['value']
        logger.info('Set hotkey-show-app to %s' % hotkey)

        # Bind a new key
        from ulauncher.ui.windows.UlauncherWindow import UlauncherWindow
        ulauncher_window = UlauncherWindow.get_instance()
        ulauncher_window.bind_show_app_hotkey(hotkey)
        self.settings.set_property('hotkey-show-app', hotkey)
        self.settings.save_to_file()

    @rt.route('/set/theme-name')
    @glib_idle_add
    def prefs_set_theme_name(self, url_params):
        name = url_params['query']['value']
        logger.info('Set theme-name to %s' % name)

        self.settings.set_property('theme-name', name)
        self.settings.save_to_file()

        from ulauncher.ui.windows.UlauncherWindow import UlauncherWindow
        ulauncher_window = UlauncherWindow.get_instance()
        ulauncher_window.init_theme()

    @rt.route('/show/hotkey-dialog')
    @glib_idle_add
    def prefs_showhotkey_dialog(self, url_params):
        self._hotkey_name = url_params['query']['name']
        logger.info('Show hotkey-dialog for %s' % self._hotkey_name)
        self.hotkey_dialog.present()

    @rt.route('/set/clear-previous-query')
    def prefs_set_clear_previous_text(self, url_params):
        is_on = self._get_bool(url_params['query']['value'])
        logger.info('Set clear-previous-query to %s' % is_on)
        self.settings.set_property('clear-previous-query', is_on)
        self.settings.save_to_file()

    @rt.route('/set/blacklisted-desktop-dirs')
    def prefs_set_blacklisted_desktop_dirs(self, url_params):
        dirs = url_params['query']['value']
        logger.info('Set blacklisted-desktop-dirs to %s' % dirs)
        self.settings.set_property('blacklisted-desktop-dirs', dirs)
        self.settings.save_to_file()

    @rt.route('/show/file-browser')
    @glib_idle_add
    def prefs_show_file_browser(self, url_params):
        """
        Request params: type=(image|all), name=(str)
        """
        file_browser_name = url_params['query']['name']
        logger.info('Show file browser dialog for %s' % file_browser_name)
        dialog = Gtk.FileChooserDialog("Please choose a file", self, Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        filter_images = Gtk.FileFilter()
        filter_images.set_name("Image files")
        filter_images.add_mime_type("image/*")
        dialog.add_filter(filter_images)

        response = dialog.run()
        data = {
            'value': None
        }
        if response == Gtk.ResponseType.OK:
            data['value'] = dialog.get_filename()

        logger.debug('%s %s' % (file_browser_name, data))
        self.send_webview_notification(file_browser_name, data)
        dialog.destroy()

    @rt.route('/open/web-url')
    def prefs_open_url(self, url_params):
        url = unquote(url_params['query']['url'])
        logger.info('Open Web URL %s' % url)
        OpenUrlAction(url).run()

    @rt.route('/close')
    def prefs_close(self, url_params):
        logger.info('Close preferences')
        self.hide()

    @rt.route('/shortcut/get-all')
    def prefs_shortcut_get_all(self, url_params):
        logger.info('Handling /shortcut/get-all')
        shortcuts = ShortcutsDb.get_instance()
        return shortcuts.get_sorted_records()

    @rt.route('/shortcut/update')
    @rt.route('/shortcut/add')
    def prefs_shortcut_update(self, url_params):
        req_data = url_params['query']
        logger.info('Add/Update shortcut: %s' % json.dumps(req_data))
        shortcuts = ShortcutsDb.get_instance()
        id = shortcuts.put_shortcut(force_unicode(req_data['name']),
                                    force_unicode(req_data['keyword']),
                                    force_unicode(req_data['cmd']),
                                    req_data.get('icon') or None,
                                    str_to_bool(req_data['is_default_search']),
                                    req_data.get('id'))
        shortcuts.commit()
        return {'id': id}

    @rt.route('/shortcut/remove')
    def prefs_shortcut_remove(self, url_params):
        req_data = url_params['query']
        logger.info('Remove shortcut: %s' % json.dumps(req_data))
        shortcuts = ShortcutsDb.get_instance()
        shortcuts.remove(req_data['id'])
        shortcuts.commit()

    @rt.route('/extension/get-all')
    def prefs_extension_get_all(self, url_params):
        logger.info('Handling /extension/get-all')
        return self._get_all_extensions()

    @rt.route('/extension/add')
    def prefs_extension_add(self, url_params):
        url = url_params['query']['url']
        logger.info('Add extension: %s' % url)
        downloader = ExtensionDownloader.get_instance()
        try:
            ext_id = downloader.download(url)
            ExtensionRunner.get_instance().run(ext_id)
        except (ExtensionDownloaderError, ManifestValidationError) as e:
            raise PrefsApiError(e.message)

        return self._get_all_extensions()

    @rt.route('/extension/update-prefs')
    def prefs_extension_update_prefs(self, url_params):
        query = url_params['query']
        ext_id = query['id']
        logger.info('Update extension preferences: %s' % query)
        prefix = 'pref.'
        controller = ExtensionServer.get_instance().get_controller(ext_id)
        preferences = [(key[len(prefix):], value) for key, value in query.items() if key.startswith(prefix)]
        for pref_id, value in preferences:
            old_value = controller.preferences.get(pref_id)['value']
            controller.preferences.set(pref_id, value)
            if value != old_value:
                controller.trigger_event(PreferencesUpdateEvent(pref_id, old_value, value))

    @rt.route('/extension/check-updates')
    def prefs_extension_check_updates(self, url_params):
        logger.info('Handling /extension/check-updates')
        ext_id = url_params['query']['id']
        try:
            return ExtensionDownloader.get_instance().get_new_version(ext_id)
        except ExtensionIsUpToDateError as e:
            pass

    @rt.route('/extension/update-ext')
    def prefs_extension_update_ext(self, url_params):
        ext_id = url_params['query']['id']
        logger.info('Update extension: %s' % ext_id)
        downloader = ExtensionDownloader.get_instance()
        try:
            downloader.update(ext_id)
        except ManifestValidationError as e:
            raise PrefsApiError(e.message)

    @rt.route('/extension/remove')
    def prefs_extension_remove(self, url_params):
        ext_id = url_params['query']['id']
        logger.info('Remove extension: %s' % ext_id)
        downloader = ExtensionDownloader.get_instance()
        downloader.remove(ext_id)

    ######################################
    # Helpers
    ######################################

    def _get_all_extensions(self):
        extensions = []
        for ext_id, _ in find_extensions(EXTENSIONS_DIR):
            prefs = ExtensionPreferences.create_instance(ext_id)
            prefs.manifest.refresh()
            try:
                prefs.manifest.validate()
                prefs.manifest.check_compatibility()
            except (ManifestValidationError, VersionIncompatibilityError):
                continue
            extensions.append(self._get_extension_info(ext_id, prefs))

        return extensions

    def _get_extension_info(self, ext_id, prefs):
        ext_db = ExtensionDb.get_instance()
        ext_db_record = ext_db.find(ext_id, {})
        return {
            'id': ext_id,
            'url': ext_db_record.get('url'),
            'updated_at': ext_db_record.get('updated_at'),
            'last_commit': ext_db_record.get('last_commit'),
            'last_commit_time': ext_db_record.get('last_commit_time'),
            'name': prefs.manifest.get_name(),
            'icon': prefs.manifest.get_icon_path(),
            'description': prefs.manifest.get_description(),
            'developer_name': prefs.manifest.get_developer_name(),
            'preferences': prefs.get_items()
        }

    def _load_prefs_html(self, page=''):
        uri = "file://%s#/%s" % (get_data_file('preferences', 'dist', 'index.html'), page)
        self.webview.load_uri(uri)

    def _get_bool(self, str_val):
        return str(str_val).lower() in ('true', '1', 'on')

    def get_app_hotkey(self):
        app_hotkey_current_accel_name = self.settings.get_property('hotkey-show-app')
        try:
            (key, mode) = Gtk.accelerator_parse(app_hotkey_current_accel_name)
        except Exception:
            logger.warning('Unable to parse accelerator "%s". Use Ctrl+Space' % app_hotkey_current_accel_name)
            (key, mode) = Gtk.accelerator_parse("<Primary>space")
        return Gtk.accelerator_get_label(key, mode)

    def on_hotkey_set(self, widget, hotkey_val, hotkey_display_val):
        self.send_webview_notification(self._hotkey_name, {
            'value': hotkey_val,
            'displayValue': hotkey_display_val
        })


def str_to_bool(value):
    return value in [1, 'true', 'True']
