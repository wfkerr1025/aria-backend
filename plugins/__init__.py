import os
import json
from .plugin_manager import PluginManager
# Global plugin manager instance
plugin_manager = PluginManager(os.path.join(os.getcwd(), "plugins"))
