from calibre.customize import InterfaceActionBase


class KoboHighlightKeeperPlugin(InterfaceActionBase):
    name = 'Kobo Highlight Keeper'
    description = ('Keep Kobo highlights, notes and bookmarks in calibre (epub and kepub) and '
                   'restore them to the Kobo after a book is deleted, re-sent or re-converted.')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Ebran Bright'
    version = (0, 1, 0)
    minimum_calibre_version = (6, 0, 0)
    actual_plugin = 'calibre_plugins.kobo_highlight_keeper.action:KoboHighlightKeeperAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.kobo_highlight_keeper.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
