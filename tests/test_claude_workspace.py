from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.claude_workspace import WorkspaceContext, claude_workspace


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.subprocess.run')
def test_claude_workspace_clones_without_token(mock_run, mock_gitauth, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')

    with claude_workspace('owner/repo', ref=None) as ws:
        assert ws.repo == 'owner/repo'
        assert ws.ref is None

    # First call was clone
    clone_call = mock_run.call_args_list[0]
    cmd = clone_call.args[0]
    assert cmd[0:3] == ['git', 'clone', '--filter=blob:none']
    assert 'https://github.com/owner/repo.git' in cmd
    # No GitAuth when no token
    mock_gitauth.assert_not_called()


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.subprocess.run')
def test_claude_workspace_sets_up_gitauth_when_token(mock_run, mock_gitauth, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')
    auth_instance = MagicMock()
    auth_instance.env.return_value = {'GIT_ASKPASS': '/tmp/askpass'}
    mock_gitauth.return_value = auth_instance

    with claude_workspace('owner/repo', ref=None, token='ghp_xxx') as ws:
        assert ws.git_env.get('GIT_ASKPASS') == '/tmp/askpass'

    mock_gitauth.assert_called_once()
    auth_instance.__enter__.assert_called_once()
    auth_instance.cleanup.assert_called_once()


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.subprocess.run')
def test_claude_workspace_checks_out_ref(mock_run, mock_gitauth, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')

    with claude_workspace('owner/repo', ref='feature-branch'):
        pass

    cmds = [call.args[0] for call in mock_run.call_args_list]
    assert ['git', 'clone', '--filter=blob:none', 'https://github.com/owner/repo.git'] == cmds[0][:4]
    assert ['git', 'fetch', '--depth', '50', 'origin', 'feature-branch'] in cmds
    assert ['git', 'checkout', '--detach', 'FETCH_HEAD'] in cmds


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.subprocess.run')
def test_claude_workspace_with_clone_depth(mock_run, mock_gitauth, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')

    with claude_workspace('owner/repo', ref=None, clone_depth=1):
        pass

    clone_cmd = mock_run.call_args_list[0].args[0]
    assert '--depth' in clone_cmd
    depth_idx = clone_cmd.index('--depth')
    assert clone_cmd[depth_idx + 1] == '1'


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.subprocess.run')
def test_capture_diff_adds_intent_to_add_then_diffs(mock_run, mock_gitauth, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='the-diff', stderr='')

    with claude_workspace('owner/repo', ref=None) as ws:
        mock_run.reset_mock()
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='the-diff', stderr='')
        diff = ws.capture_diff()

    assert diff == 'the-diff'
    cmds = [call.args[0] for call in mock_run.call_args_list]
    assert ['git', 'add', '-N', '.'] in cmds
    assert ['git', 'diff', '--binary'] in cmds


@patch('scripts.claude_workspace.GitAuth')
@patch('scripts.claude_workspace.shutil.rmtree')
@patch('scripts.claude_workspace.subprocess.run')
def test_cleanup_happens_on_exception(mock_run, mock_rmtree, mock_gitauth):
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')

    auth_instance = MagicMock()
    mock_gitauth.return_value = auth_instance

    with pytest.raises(RuntimeError):
        with claude_workspace('owner/repo', ref=None, token='ghp_xxx'):
            raise RuntimeError('boom')

    auth_instance.cleanup.assert_called_once()
    mock_rmtree.assert_called_once()
