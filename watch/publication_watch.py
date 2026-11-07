#!/usr/bin/env python3
"""Read-only Git/Netlify inventory; writes only its own reports and event receipts.

No checkout, stage, commit, merge, push, build, install, or deploy operations.
Git fetches, when needed, download objects only (no refs or FETCH_HEAD).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import uuid

BASE = Path(__file__).resolve().parent
ENV = {**os.environ, 'PATH': '/opt/homebrew/bin:/usr/bin:/bin',
       'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never',
       'GIT_OPTIONAL_LOCKS': '0', 'NETLIFY_CLI_DISABLE_UPDATE_NOTIFIER': 'true'}


class CheckError(Exception):
    pass


def run(args, cwd=None, timeout=35, allow=(0,)):
    try:
        p = subprocess.run(args, cwd=cwd, env=ENV, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(type(exc).__name__) from None
    if p.returncode not in allow:
        # Never persist command stderr: tools may include credentials or URLs.
        raise CheckError(f'{Path(args[0]).name} exit {p.returncode}')
    return p.stdout


def git(path, *args, **kw):
    return run(['/usr/bin/git', '-C', str(path), *args], **kw)


def atomic(path, obj):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(obj, out, indent=2, sort_keys=True)
        out.write('\n')
    os.replace(temp, path)
    path.chmod(0o600)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def worktrees(text):
    """-z porcelain supports spaces, newlines, and quoted-looking path names."""
    return [s[len('worktree '):] for s in text.split('\0') if s.startswith('worktree ')]


def github_repo(remote):
    m = re.search(r'(?:github\.com[:/])([\w.-]+/[\w.-]+?)(?:\.git)?$', remote)
    return m.group(1) if m else None


def live_heads(path):
    text = git(path, 'ls-remote', '--symref', 'origin', 'HEAD', 'refs/heads/*')
    default = None
    heads = {}
    for line in text.splitlines():
        if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
            default = line.split('\t')[0][len('ref: refs/heads/'):]
        elif '\trefs/heads/' in line:
            sha, ref = line.split('\t', 1)
            heads[ref[len('refs/heads/'):]] = sha
    if not default or default not in heads:
        raise CheckError('Remote default branch unavailable')
    return default, heads


def ensure_object(path, sha):
    try:
        git(path, 'cat-file', '-e', sha + '^{commit}')
    except CheckError:
        git(path, 'fetch', '--quiet', '--no-tags', '--no-write-fetch-head',
            '--refmap=', 'origin', sha, timeout=60)


def ahead(path, base, head):
    ensure_object(path, base)
    return int(git(path, 'rev-list', '--count', base + '..' + head).strip())


def unmerged(path, default_sha, branch_sha):
    """Patch equivalence avoids labelling squash/cherry-picked work unmerged."""
    ensure_object(path, default_sha)
    patches = sum(line.startswith('+ ') for line in
                  git(path, 'cherry', default_sha, branch_sha).splitlines())
    # A merge can contain conflict-resolution work with no ordinary patch commit.
    merges = git(path, 'rev-list', '--merges', default_sha + '..' + branch_sha).splitlines()
    if merges and git(path, 'diff', '--name-only', default_sha, branch_sha).strip():
        patches += len(merges)
    return patches


def inspect_repo(project, config):
    path = project['path']
    result = {'name': project['title'], 'path': path, 'worktrees': [],
              'branches': [], 'pull_requests': [], 'errors': []}
    try:
        for wt in worktrees(git(path, 'worktree', 'list', '--porcelain', '-z')):
            try:
                status = git(wt, 'status', '--porcelain=v1', '-z', '--untracked-files=normal')
                # Rename/copy has an extra NUL-delimited old path, not another change.
                records = status.split('\0'); entries = []; i = 0
                while i < len(records):
                    row = records[i]; i += 1
                    if not row:
                        continue
                    entries.append(row[3:])
                    if 'R' in row[:2] or 'C' in row[:2]:
                        i += 1
                branch_name = git(wt, 'symbolic-ref', '-q', '--short', 'HEAD', allow=(0, 1)).strip()
                head = git(wt, 'rev-parse', '--verify', 'HEAD').strip()
                result['worktrees'].append({'path': wt, 'dirty_count': len(entries),
                                            'dirty_paths': sorted(entries), 'head': head,
                                            'branch': branch_name or None})
            except CheckError as exc:
                result['errors'].append('Worktree unreadable: ' + str(exc))
        try:
            git(path, 'remote', 'get-url', 'origin')
        except CheckError:
            result['errors'].append('No origin remote configured; push/merge coverage unknown')
            return result
        default, heads = live_heads(path)
        for sha in set(heads.values()):
            ensure_object(path, sha)
        result['default_branch'] = default
        result['remote_default_sha'] = heads[default]
        rows = git(path, 'for-each-ref', '--format=%(refname:short)%09%(objectname)%09%(upstream:remotename)%09%(upstream:remoteref)', 'refs/heads/').splitlines()
        for row in rows:
            name, sha, upstream_remote, upstream_ref = row.split('\t')
            branch = {'name': name, 'sha': sha, 'unpushed': None, 'unmerged': None}
            try:
                # Compare to the actual push destination, not an out-of-date tracking ref.
                remote_name = upstream_ref.removeprefix('refs/heads/') if upstream_remote == 'origin' else name
                remote_sha = heads.get(remote_name)
                branch['remote_branch'] = remote_name
                # A deleted remote branch isn't unpublished if its commits already
                # live on main or another origin branch. Use live refs, not stale refs.
                branch['unpushed'] = int(git(path, 'rev-list', '--count', sha,
                    '--not', *sorted(set(heads.values()))).strip())
                branch['remote_missing'] = remote_sha is None
                branch['unmerged'] = 0 if name == default else unmerged(path, heads[default], sha)
            except CheckError as exc:
                branch['error'] = str(exc)
            result['branches'].append(branch)
        branch_shas = {b['sha'] for b in result['branches']}
        for wt in result['worktrees']:
            if wt['branch'] or wt['head'] in branch_shas:
                continue
            branch = {'name': 'detached at ' + wt['path'], 'sha': wt['head'],
                      'unpushed': None, 'unmerged': None, 'detached': True}
            try:
                branch['unpushed'] = int(git(path, 'rev-list', '--count', wt['head'],
                    '--not', *sorted(set(heads.values()))).strip())
                branch['unmerged'] = unmerged(path, heads[default], wt['head'])
            except CheckError as exc:
                branch['error'] = str(exc)
            result['branches'].append(branch)
            branch_shas.add(wt['head'])
        local_names = {b['name'] for b in result['branches']}
        for name, sha in heads.items():
            if name == default or name in local_names:
                continue
            branch = {'name': 'origin/' + name, 'sha': sha, 'unpushed': 0,
                      'unmerged': None, 'remote_only': True}
            try:
                branch['unmerged'] = unmerged(path, heads[default], sha)
            except CheckError as exc:
                branch['error'] = str(exc)
            result['branches'].append(branch)
        repo = github_repo(git(path, 'remote', 'get-url', 'origin').strip())
        if repo:
            result['github_repo'] = repo
            try:
                result['pull_requests'] = json.loads(run([config['gh_bin'], 'pr', 'list', '-R', repo,
                    '--state', 'open', '--limit', '100', '--json', 'number,url,headRefName,isDraft']))
                if len(result['pull_requests']) == 100:
                    result['errors'].append('PR list capped at 100; additional PRs unknown')
            except (CheckError, ValueError) as exc:
                result['errors'].append('Open PR check unavailable: ' + type(exc).__name__)
    except (CheckError, ValueError, OSError) as exc:
        result['errors'].append(str(exc) if isinstance(exc, CheckError) else type(exc).__name__)
    return result


def api(config, operation, data):
    return json.loads(run([config['netlify_bin'], 'api', operation, '--data', json.dumps(data)], timeout=45))


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def desired_files(site):
    """Reproduce only pure publisher transforms in memory; NEVER call build/deploy/check.

    This also notices source changes which haven't reached the staging folder.
    """
    spec = importlib.util.spec_from_file_location('site_publication_readonly', site['local_adapter'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = {}
    for name, source in module.SOURCES.items():
        if name in module.TRANSFORMED:
            data = module.transform(module.read_text(source), name).encode('utf-8')
        else:
            data = Path(source).read_bytes()
        files['/' + name] = sha1(data)
    files['/index.html'] = sha1(module.landing_page().encode('utf-8'))
    # _headers is deployment configuration; Netlify doesn't expose it as a site file.
    functions = {name: hashlib.sha256((Path(module.FUNCTIONS) / name).read_bytes()).hexdigest()
                 for name in module.FUNCTION_FILES}
    return files, functions, Path(module.DEPLOY)


def inspect_site(site, config):
    result = {'name': site['name'], 'url': site['url'], 'id': site['id'],
              'different_from_live': [], 'unbuilt_changes': [], 'unpublished_deploys': [],
              'functions_changed': [], 'errors': []}
    try:
        info = api(config, 'getSite', {'site_id': site['id']})
        published = info.get('published_deploy') or {}
        result['published_id'] = published.get('id')
        result['published_at'] = published.get('published_at')
        result['published_commit'] = published.get('commit_ref')
        if not published.get('id'):
            result['errors'].append('No production publication found')
        live = {r['path']: r['sha'] for r in api(config, 'listSiteFiles', {'site_id': site['id']})}
        # Walk recent deploys; production, branch and draft versions are separate evidence.
        deploys = api(config, 'listSiteDeploys', {'site_id': site['id'], 'per_page': 100})
        pub_time = published.get('created_at') or ''
        result['unpublished_deploys'] = [
            {k: d.get(k) for k in ('id', 'state', 'context', 'branch', 'commit_ref', 'created_at', 'draft')}
            for d in deploys if d.get('id') != published.get('id')
            and ((d.get('created_at') or '') > pub_time
                 or (d.get('state') == 'ready' and not d.get('published_at')))
        ]
        if len(deploys) == 100:
            result['errors'].append('Only the latest 100 deploys checked; older unpublished versions unknown')
        if site.get('local_adapter'):
            desired, functions, staged_dir = desired_files(site)
            result['different_from_live'] = sorted(p for p, digest in desired.items() if live.get(p) != digest)
            result['unbuilt_changes'] = sorted(p for p, digest in desired.items()
                if not (staged_dir / p.lstrip('/')).is_file()
                or sha1((staged_dir / p.lstrip('/')).read_bytes()) != digest)
            receipt = read_json(site['last_deployed'], {})
            if receipt.get('deploy_id') == published.get('id'):
                result['functions_changed'] = sorted(n for n, digest in functions.items()
                    if (receipt.get('manifest') or {}).get('functions/' + n) != digest)
            else:
                result['errors'].append('Function-source comparison unknown: local receipt differs from live deploy')
        else:
            result['errors'].append('Local publish source not mapped; local freshness unknown')
    except Exception as exc:
        # Publisher transformations can raise their own Fail class. Keep that
        # scope unknown without crashing the independent Git checks.
        result['errors'].append(str(exc) if isinstance(exc, CheckError) else type(exc).__name__)
    return result


def inventory_sites(config):
    """Discover accessible sites without storing API credentials or build environment."""
    mapped = {s['id']: s for s in config['netlify_sites']}
    found = []
    page = 1
    while True:
        rows = api(config, 'listSites', {'page': page, 'per_page': 100})
        for s in rows:
            found.append(mapped.get(s['id']) or {'id': s['id'], 'name': s['name'],
                                               'url': s.get('ssl_url') or s.get('url')})
        if len(rows) < 100:
            return found
        page += 1


def findings(report):
    out = []
    for repo in report['repos']:
        name = repo['name']
        for wt in repo['worktrees']:
            if wt['dirty_count']:
                out.append({'key': 'dirty:' + wt['path'], 'text': f"{name}: {wt['dirty_count']} uncommitted/untracked changes in `{wt['path']}`"})
        for b in repo['branches']:
            bits = []
            if b['unpushed']:
                bits.append('not on remote' if b.get('remote_missing') else f"{b['unpushed']} unpushed commits")
            if b['unmerged']:
                bits.append(f"{b['unmerged']} patches not merged into remote {repo['default_branch']}")
            if bits:
                out.append({'key': 'branch:' + repo['path'] + ':' + b['name'], 'text': f"{name} `{b['name']}`: " + '; '.join(bits)})
            if b.get('error'):
                out.append({'key': 'branch-unknown:' + repo['path'] + ':' + b['name'], 'text': f"{name} `{b['name']}`: comparison UNKNOWN ({b['error']})"})
        for pr in repo['pull_requests']:
            out.append({'key': 'pr:' + pr['url'], 'text': f"{name}: open {'draft ' if pr['isDraft'] else ''}[PR {pr['number']}]({pr['url']})"})
        if repo['errors']:
            out.append({'key': 'repo-unknown:' + repo['path'], 'text': f"{name}: coverage UNKNOWN — " + '; '.join(repo['errors'])})
    for site in report['sites']:
        prefix = f"[{site['name']}]({site['url']})"
        for field, description in [('different_from_live', 'local publication files differ from production'),
                                   ('unbuilt_changes', 'source files differ from the staged build'),
                                   ('functions_changed', 'function sources changed since the confirmed live receipt')]:
            if site[field]:
                out.append({'key': field + ':' + site['id'], 'text': prefix + ': ' + description + ' — ' + ', '.join(site[field])})
        if site['unpublished_deploys']:
            out.append({'key': 'unpublished:' + site['id'], 'text': prefix + f": {len(site['unpublished_deploys'])} unpublished ready variants or newer non-live attempts (ready previews, pending builds and failures kept distinct in the JSON report; creation time alone does not prove newer content)"})
        if site['errors']:
            out.append({'key': 'site-unknown:' + site['id'], 'text': prefix + ': coverage UNKNOWN — ' + '; '.join(site['errors'])})
    for error in report.get('errors', []):
        out.append({'key': 'inventory-unknown', 'text': 'Netlify inventory UNKNOWN — ' + error})
    return out


def fingerprint(items):
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def save_report(report):
    atomic(BASE / 'publication-report.json', report)
    lines = ['# Git and Netlify monitoring tasks', '',
             'Read-only checks every 30 minutes while this Mac is available. Monitoring does not authorize committing, merging, pushing or deploying.', '',
             f"Last checked: {report['checked_at']}. {len(report['repos'])} repository roots, {len(report['sites'])} accessible Netlify sites.", '',
             'Changes during active work and deliberately retained branches are inventory, not instructions to publish. Different bytes prove a difference, not that every local variant should replace production. Unknown coverage never counts as current.', '',
             '## Current findings', '']
    lines += ['- [ ] ' + f['text'].replace('\n', ' ') for f in report['findings']] or ['No pending differences found in checked scope.']
    lines += ['', '## Resolved since the previous check', '']
    lines += ['- [x] ' + x for x in report.get('resolved', [])] or ['None.']
    lines += ['', '## Coverage', '',
              'Registered code projects in publication-config.json plus all their Git worktrees and local branches; live origin branch refs and open GitHub PRs. Projects under a standing hold are left out of the config. New Netlify sites are discovered automatically; unmapped local sources are reported unknown.', '',
              "Mapped production pages use Netlify's uploaded-file SHA1 values compared with the existing publisher's in-memory transformations of current local sources, including files not rebuilt yet. Function sources use the matching live deployment receipt. Publisher schedules are unchanged.", '',
              'New or resolved findings wake the guardian once per changed summary. Unchanged findings stay on this list without repeated alerts. API/auth/network or local permission errors remain explicit unknowns; this monitor never signs in or changes credentials.', '']
    path = BASE / 'publication-tasks.md'
    temp = path.with_suffix('.md.new')
    temp.write_text('\n'.join(lines)); temp.chmod(0o600); os.replace(temp, path)


def send_event(config, report):
    route = read_json(config['guardian_config'])
    eid, iid = str(uuid.uuid4()), str(uuid.uuid4())
    payload = {'event_id': eid, 'kind': 'publication_health', 'incidents': [
        {'id': iid, 'code': 'publication_status', 'root': route['guardian_thread_id'],
         'report_path': str(BASE / 'publication-tasks.md'),
         'finding_count': len(report['findings'])}], 'more_pending': 0}
    request = urllib.request.Request(route['webhook_url'], json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in (200, 201, 202, 204):
            raise CheckError('Guardian delivery rejected')
    return {'event_id': eid, 'incident_id': iid, 'at': time.time()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prime', action='store_true', help='Record initial findings without waking this active setup turn')
    parser.add_argument('--config', default=str(BASE / 'publication-config.json'))
    args = parser.parse_args(argv)
    lock_file = BASE / 'publication.lock'
    with lock_file.open('a') as lock:
        lock_file.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Publication check already running'); return 0
        started = time.monotonic()
        config = read_json(args.config)
        previous = read_json(BASE / 'publication-state.json', {})
        with ThreadPoolExecutor(max_workers=4) as pool:
            repos = list(pool.map(lambda p: inspect_repo(p, config), config['projects']))
        report = {'checked_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                  'repos': repos, 'sites': [], 'errors': []}
        try:
            report['sites'] = [inspect_site(s, config) for s in inventory_sites(config)]
        except (CheckError, ValueError, OSError) as exc:
            report['errors'].append(str(exc) if isinstance(exc, CheckError) else type(exc).__name__)
        report['findings'] = findings(report)
        keys = {f['key'] for f in report['findings']}
        report['resolved'] = [f['text'] for f in previous.get('findings', []) if f['key'] not in keys]
        report['duration_seconds'] = round(time.monotonic() - started, 2)
        save_report(report)
        signature = fingerprint(report['findings'])
        state = {'checked_at': report['checked_at'], 'findings': report['findings'],
                 'last_notified': previous.get('last_notified'),
                 'last_delivery': previous.get('last_delivery')}
        if args.prime:
            state['last_notified'] = signature
        elif signature != state['last_notified']:
            try:
                state['last_delivery'] = send_event(config, report)
                state['last_notified'] = signature
            except Exception as exc:
                state['delivery_error'] = type(exc).__name__
        atomic(BASE / 'publication-state.json', state)
        print(json.dumps({'repositories': len(repos), 'sites': len(report['sites']),
                          'findings': len(report['findings']), 'seconds': report['duration_seconds'],
                          'delivery_error': state.get('delivery_error')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
