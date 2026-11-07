"""Outcome tests use isolated local repositories and saved API responses; no real writes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publication_watch', Path(__file__).with_name('publication_watch.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class RepoOutcomes(unittest.TestCase):
    def test_actual_git_dirty_unpushed_unmerged_and_deleted_branch(self):
        with tempfile.TemporaryDirectory(prefix='publication-test-') as temp:
            root = Path(temp); repo = root / 'working tree'; remote = root / 'origin.git'
            def cmd(*args, cwd=None):
                return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout
            cmd('git', 'init', '--bare', '--initial-branch=main', str(remote))
            cmd('git', 'init', '--initial-branch=main', str(repo))
            cmd('git', 'config', 'user.name', 'Monitor test', cwd=repo)
            cmd('git', 'config', 'user.email', 'monitor@example.invalid', cwd=repo)
            cmd('git', 'remote', 'add', 'origin', str(remote), cwd=repo)
            def commit(name):
                (repo / name).write_text(name)
                cmd('git', 'add', name, cwd=repo); cmd('git', 'commit', '-m', name, cwd=repo)
            commit('base'); cmd('git', 'push', 'origin', 'main', cwd=repo)
            cmd('git', 'switch', '-c', 'retained', cwd=repo); commit('merged')
            cmd('git', 'switch', 'main', cwd=repo); cmd('git', 'merge', '--ff-only', 'retained', cwd=repo)
            cmd('git', 'push', 'origin', 'main', cwd=repo)
            cmd('git', 'switch', '-c', 'feature', cwd=repo); commit('feature')
            cmd('git', 'push', 'origin', 'feature:remote-only', cwd=repo)
            commit('local-only')
            (repo / 'base').write_text('dirty')
            (repo / 'untracked file').write_text('untracked')
            # A newer remote main is not in the local object store: monitoring
            # must fetch comparison objects without moving any checkout/ref.
            writer = root / 'writer'
            cmd('git', 'clone', str(remote), str(writer))
            cmd('git', 'config', 'user.name', 'Monitor test', cwd=writer)
            cmd('git', 'config', 'user.email', 'monitor@example.invalid', cwd=writer)
            (writer / 'remote-new').write_text('remote')
            cmd('git', 'add', 'remote-new', cwd=writer)
            cmd('git', 'commit', '-m', 'remote newer', cwd=writer)
            cmd('git', 'push', 'origin', 'main', cwd=writer)
            detached = root / 'detached'
            cmd('git', 'worktree', 'add', '--detach', str(detached), 'feature', cwd=repo)
            (detached / 'detached-only').write_text('saved detached work')
            cmd('git', 'add', 'detached-only', cwd=detached)
            cmd('git', 'commit', '-m', 'detached work', cwd=detached)
            before_head = cmd('git', 'rev-parse', 'HEAD', cwd=repo)
            before_status = cmd('git', 'status', '--porcelain', cwd=repo)
            before_main = cmd('git', 'rev-parse', 'main', cwd=repo)
            result = w.inspect_repo({'path': str(repo), 'title': 'Fixture'}, {'gh_bin': 'false'})
            self.assertEqual(result['errors'], [])
            branches = {b['name']: b for b in result['branches']}
            self.assertEqual(branches['retained']['unpushed'], 0)
            self.assertEqual(branches['retained']['unmerged'], 0)
            self.assertEqual(branches['feature']['unpushed'], 1)
            self.assertEqual(branches['feature']['unmerged'], 2)
            self.assertEqual(branches['origin/remote-only']['unmerged'], 1)
            saved = next(b for b in result['branches'] if b.get('detached'))
            self.assertEqual(saved['unpushed'], 2)
            self.assertEqual(saved['unmerged'], 3)
            self.assertEqual(result['worktrees'][0]['dirty_count'], 2)
            self.assertEqual(cmd('git', 'rev-parse', 'HEAD', cwd=repo), before_head)
            self.assertEqual(cmd('git', 'status', '--porcelain', cwd=repo), before_status)
            self.assertEqual(cmd('git', 'rev-parse', 'main', cwd=repo), before_main)
            self.assertFalse((repo / '.git/FETCH_HEAD').exists())


class NetlifyOutcomes(unittest.TestCase):
    def test_event_uses_separate_coalescing_kind_and_current_guardian(self):
        with tempfile.TemporaryDirectory() as temp:
            config_file = Path(temp) / 'route.json'
            config_file.write_text(json.dumps({'guardian_thread_id': 'current-successor',
                                             'webhook_url': 'https://example.invalid/private-route'}))
            with patch.object(w.urllib.request, 'urlopen') as open_url:
                open_url.return_value.__enter__.return_value.status = 202
                result = w.send_event({'guardian_config': str(config_file)}, {'findings': []})
                request = open_url.call_args.args[0]
                payload = json.loads(request.data)
                self.assertEqual(payload['kind'], 'publication_health')
                self.assertEqual(payload['incidents'][0]['root'], 'current-successor')
                self.assertNotIn('private-route', json.dumps(payload))
                self.assertEqual(result['event_id'], payload['event_id'])

    def test_live_stale_unbuilt_function_change_and_unpublished_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'index.html').write_bytes(b'old')
            receipt = root / 'receipt.json'
            receipt.write_text(json.dumps({'deploy_id': 'live', 'manifest': {'functions/f.mjs': 'old'}}))
            site = {'id': 'test', 'name': 'Test', 'url': 'https://example.invalid',
                    'local_adapter': 'not-executed', 'last_deployed': str(receipt)}
            def api(_config, operation, _data):
                return {'getSite': {'published_deploy': {'id': 'live', 'created_at': '2026-11-01'}},
                        'listSiteFiles': [{'path': '/index.html', 'sha': w.sha1(b'old')}],
                        'listSiteDeploys': [{'id': 'live', 'created_at': '2026-11-01'},
                                            {'id': 'older-preview', 'state': 'ready', 'draft': True,
                                             'created_at': '2026-10-30'},
                                            {'id': 'preview', 'state': 'ready', 'draft': True,
                                             'created_at': '2026-11-02'}]}[operation]
            with patch.object(w, 'api', side_effect=api), patch.object(w, 'desired_files',
                    return_value=({'/index.html': w.sha1(b'new')}, {'f.mjs': 'new'}, root)):
                r = w.inspect_site(site, {})
            self.assertEqual(r['errors'], [])
            self.assertEqual(r['different_from_live'], ['/index.html'])
            self.assertEqual(r['unbuilt_changes'], ['/index.html'])
            self.assertEqual(r['functions_changed'], ['f.mjs'])
            self.assertEqual(r['unpublished_deploys'][0]['state'], 'ready')
            self.assertEqual(len(r['unpublished_deploys']), 2)
            self.assertEqual((root / 'index.html').read_bytes(), b'old')

    def test_failed_api_is_unknown_not_clean(self):
        with patch.object(w, 'api', side_effect=w.CheckError('API unavailable')):
            r = w.inspect_site({'id': 'test', 'name': 'Test', 'url': 'https://example.invalid'}, {})
        self.assertTrue(r['errors'])
        report = {'repos': [], 'sites': [r]}
        self.assertIn('UNKNOWN', w.findings(report)[0]['text'])

    def test_persisted_checks_alert_once_and_report_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); config = root / 'config.json'
            config.write_text(json.dumps({'projects': [{'path': 'fixture', 'title': 'Fixture'}]}))
            repo = {'name': 'Fixture', 'path': 'fixture', 'worktrees': [],
                    'branches': [], 'pull_requests': [], 'errors': []}
            with patch.object(w, 'BASE', root), patch.object(w, 'inspect_repo', return_value=repo), \
                    patch.object(w, 'inventory_sites', return_value=[]), \
                    patch.object(w, 'send_event', return_value={'event_id': 'fixture'}) as send:
                w.main(['--config', str(config), '--prime'])
                w.main(['--config', str(config)])
                send.assert_not_called()
                repo['worktrees'] = [{'path': 'fixture', 'dirty_count': 1, 'dirty_paths': ['changed']}]
                w.main(['--config', str(config)])
                self.assertEqual(send.call_count, 1)
                w.main(['--config', str(config)])
                self.assertEqual(send.call_count, 1)
                repo['worktrees'] = []
                w.main(['--config', str(config)])
                self.assertEqual(send.call_count, 2)
                result = json.loads((root / 'publication-report.json').read_text())
                self.assertEqual(result['findings'], [])
                self.assertEqual(len(result['resolved']), 1)

    def test_failed_delivery_retries_without_losing_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); config = root / 'config.json'
            config.write_text(json.dumps({'projects': []}))
            with patch.object(w, 'BASE', root), patch.object(w, 'inventory_sites', return_value=[]), \
                    patch.object(w, 'send_event', side_effect=[OSError('secret must not be logged'), {'event_id': 'fixture'}]) as send:
                w.main(['--config', str(config)])
                failed = json.loads((root / 'publication-state.json').read_text())
                self.assertEqual(failed['delivery_error'], 'OSError')
                self.assertNotIn('secret', json.dumps(failed))
                self.assertIsNone(failed['last_notified'])
                w.main(['--config', str(config)])
                self.assertEqual(send.call_count, 2)
                self.assertTrue(json.loads((root / 'publication-state.json').read_text())['last_notified'])


if __name__ == '__main__':
    unittest.main()
