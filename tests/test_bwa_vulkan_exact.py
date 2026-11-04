"""Opt-in native GPU oracle test: no VM, no software Vulkan, no network fetch."""
import os
from pathlib import Path
import pytest
from scripts.validate_vulkan_alignment import validate_extensions


def test_physical_vulkan_matches_stock_bwa_extension(tmp_path):
    build=os.environ.get('BWA_VULKAN_TEST_BUILD')
    if not build:pytest.skip('Set BWA_VULKAN_TEST_BUILD to the pinned native build directory')
    report=validate_extensions(Path(build),tmp_path,1000)
    assert report['all_six_outputs_identical']
