"""Run the unchanged normal Matrix helper assertions without global SDK mocks."""
import importlib.util
from pathlib import Path

path = (
    Path(__file__).resolve().parents[2]
    / "integration/test_channels_helpers_module.py"
)
spec = importlib.util.spec_from_file_location(
    "matrix_original_helper_oracles", path
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

test_matrix_md_to_html_basic = module.test_matrix_md_to_html_basic
test_matrix_md_to_html_newlines = module.test_matrix_md_to_html_newlines
test_matrix_derive_device_id = module.test_matrix_derive_device_id
