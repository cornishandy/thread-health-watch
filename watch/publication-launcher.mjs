// Same finite node-to-Python launch pattern as the existing site publishers.
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const log = root + '/publication.log';
try {if (fs.statSync(log).size > 500000) fs.renameSync(log, log + '.1');} catch {}
const out = fs.openSync(log, 'a', 0o600);
const child = spawn('/usr/bin/python3', ['-I', '-B', root + '/publication_watch.py'], {
 stdio: ['ignore', out, out],
 env: {PATH:'/opt/homebrew/bin:/usr/bin:/bin',HOME:process.env.HOME || os.homedir(),LANG:'en_US.UTF-8'}
});
for (const signal of ['SIGTERM','SIGINT','SIGHUP']) process.on(signal, () => child.kill(signal));
child.on('error', () => process.exit(1));
child.on('exit', (code) => process.exit(code ?? 1));
