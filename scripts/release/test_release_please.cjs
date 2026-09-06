// Exercise the exact Release Please library embedded in the pinned GitHub action.
// Run with: node scripts/release/test_release_please.cjs /path/to/release-please
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(process.argv[2]);
const { PrereleaseVersioningStrategy } = require(path.join(root, 'build/src/versioning-strategies/prerelease'));
const { Version } = require(path.join(root, 'build/src/version'));
assert.equal(require(path.join(root, 'package.json')).version, '17.3.0');
const config = JSON.parse(fs.readFileSync('release-please-config.json', 'utf8')).packages['.'];
assert.equal(config.versioning, 'prerelease');
assert.equal(config.draft, true);
assert.equal(config['force-tag-creation'], true);
const commits = [{ type: 'fix', notes: [], breaking: false }];
const beta = new PrereleaseVersioningStrategy({ prerelease: config.prerelease, prereleaseType: config['prerelease-type'] });
for (const [before, after] of [['0.2.0-beta.0','0.2.0-beta.1'], ['0.2.0-beta.9','0.2.0-beta.10'], ['0.2.0','0.2.1-beta.0']]) {
  assert.equal(beta.bump(Version.parse(before), commits).toString(), after);
}
const stable = new PrereleaseVersioningStrategy({ prerelease: false, prereleaseType: config['prerelease-type'] });
assert.equal(stable.bump(Version.parse('0.2.0-beta.10'), commits).toString(), '0.2.0');
console.log('Release Please 17.3.0 beta ordering and stable transition passed');
