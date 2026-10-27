"""Single/summary equivalence, incremental publication and existing API contracts."""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from app_jobs import validate_url,submit,worker,status
from app_reports import parse_report
from app_library import AppKnowledge
from import_app import import_report,normalized_run,records
from test_app_links import TextEmbed,DEP,app_kb_client
from test_core import Vectors

BASE='https://reports.example.test/files/reports/sample-batch-c/'


if __name__=='__main__':unittest.main()
