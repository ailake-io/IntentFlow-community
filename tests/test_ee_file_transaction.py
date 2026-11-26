

def test_locked_root_explains_unwritable_project_dir(tmp_path):
    import os, pytest
    from intentflow.enterprise.file_transaction import locked_root
    if os.getuid() == 0:
        pytest.skip("root bypasses permissions")
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(ValueError, match="not writable"), locked_root(tmp_path):
            pass
    finally:
        tmp_path.chmod(0o755)
