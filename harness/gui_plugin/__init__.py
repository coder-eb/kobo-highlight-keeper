from calibre.customize import InterfaceActionBase


class KHKHarnessPlugin(InterfaceActionBase):
    """Test-only: drives a scripted scenario in the sandbox calibre. Never install for real use."""
    name = 'KHK Test Harness'
    description = 'Scripted GUI scenario + screenshots for Kobo Highlight Keeper (sandbox only)'
    supported_platforms = ['osx', 'linux', 'windows']
    author = 'Ebran Bright'
    version = (0, 1, 0)
    minimum_calibre_version = (6, 0, 0)
    actual_plugin = 'calibre_plugins.khk_harness.scenario:HarnessAction'
