import pytest
import os
from pathlib import Path
from src.core.env import parse_dotenv, load_repo_dotenv

def test_parse_dotenv():
    text = """
# This is a comment
export MY_VAR=123
OTHER_VAR=abc
QUOTED_VAR="hello world"
SINGLE_QUOTED='single'
EMPTY_VAR=
"""
    result = parse_dotenv(text)
    assert result == {
        "MY_VAR": "123",
        "OTHER_VAR": "abc",
        "QUOTED_VAR": "hello world",
        "SINGLE_QUOTED": "single",
        "EMPTY_VAR": ""
    }

def test_load_repo_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_ENV_VAR=12345\nexport TEST_ENV_VAR2=abc")
    
    # ensure it's not set
    os.environ.pop("TEST_ENV_VAR", None)
    os.environ.pop("TEST_ENV_VAR2", None)

    loaded_path = load_repo_dotenv(filename=".env", start=tmp_path)
    
    assert loaded_path == env_file
    assert os.environ.get("TEST_ENV_VAR") == "12345"
    assert os.environ.get("TEST_ENV_VAR2") == "abc"
    
    # cleanup
    os.environ.pop("TEST_ENV_VAR", None)
    os.environ.pop("TEST_ENV_VAR2", None)
