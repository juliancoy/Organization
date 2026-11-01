import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts.swarm_node import main


def test_swarm_node_routes_runtime_to_single_supervisor(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'launcher_peer_supervisor',SimpleNamespace(main=called))
    assert main(['run','--prefix','node-'])==['swarm-node','--prefix','node-']
    called.assert_called_once()


def test_swarm_node_exposes_existing_fleet_maintenance(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'worker_maintenance',SimpleNamespace(main=called))
    assert main(['drain','--node','machine.local','--reason','upgrade'])==['swarm-node','drain','--reason','upgrade','--node','machine.local']


def test_swarm_node_update_uses_standard_deployer(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'deploy_swarm_inputs',SimpleNamespace(main=called))
    assert main(['update','--watch'])==['swarm-node','--watch']


def test_swarm_node_enrollment_requires_explicit_target():
    with pytest.raises(SystemExit):main(['enroll'])
