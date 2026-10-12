"""Run offline test doubles only; prohibit real TCP connections."""
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
for name in ('KALSHI_API_KEY_ID','KALSHI_PRIVATE_KEY_PATH','KALSHI_PRIVATE_KEY_B64','KALSHI_PRIVATE_KEY_PEM'):
    os.environ[name]=''
os.environ['TRADING_ENABLED']='false'
os.environ['MANUAL_EXITS_ENABLED']='false'
def offline(*args, **kwargs):
    raise RuntimeError('Network access forbidden during offline trading tests')
socket.socket.connect=offline
socket.create_connection=offline
sys.exit(pytest.main(sys.argv[1:]))
