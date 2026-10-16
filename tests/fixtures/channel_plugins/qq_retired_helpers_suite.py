"""Run the unchanged normal QQ helper assertions without global SDK mocks."""
import importlib.util
from pathlib import Path

path = (
    Path(__file__).resolve().parents[2]
    / "integration/test_channels_helpers_module.py"
)
spec = importlib.util.spec_from_file_location(
    "qq_original_helper_oracles", path
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

test_qq_sanitize_text_strips_urls = module.test_qq_sanitize_text_strips_urls
test_qq_sanitize_text_plain_passthrough = (
    module.test_qq_sanitize_text_plain_passthrough
)
test_qq_sanitize_text_empty = module.test_qq_sanitize_text_empty
test_qq_aggressive_sanitize_bare_domains = (
    module.test_qq_aggressive_sanitize_bare_domains
)
test_qq_as_bool_variants = module.test_qq_as_bool_variants
test_qq_recoverable_ws_os_error = module.test_qq_recoverable_ws_os_error
