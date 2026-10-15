import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication_check', ROOT / 'tools/check_publication.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class PublicationTests(unittest.TestCase):
    def test_private_network_and_token_are_rejected(self):
        # Build synthetic values so the tests themselves contain no credential.
        private_host = '.'.join(['192', '168', '2', '5'])
        token = 'ghp_' + 'x' * 36
        kinds = {kind for _, kind in scanner.inspect_text(private_host + '\n' + token)}
        self.assertEqual(kinds, {'private-network-address', 'access-token'})

    def test_placeholder_and_local_development_addresses_are_allowed(self):
        self.assertEqual(scanner.inspect_text('https://api.example.invalid/v1\n127.0.0.1'), [])

    def test_new_private_proxy_cannot_pass_as_public_host(self):
        self.assertTrue(scanner.unapproved_hosts('https://' + 'unreviewed.proxy-host.net/v1', set()))
        self.assertEqual(scanner.unapproved_hosts('https://api.example.invalid/v1', set()), [])
        self.assertEqual(scanner.unapproved_hosts('https://github.com/org/repo', {'github.com'}), [])

    def test_layout_links_are_internal_and_valid(self):
        for relative in ('MMClaw','runtime','systemd','sql','docker/postgres'):
            path = ROOT / 'apps/finagent' / relative
            self.assertTrue(path.is_symlink())
            self.assertTrue(path.exists())
            self.assertTrue(path.resolve().is_relative_to(ROOT))


if __name__ == '__main__':
    unittest.main()
