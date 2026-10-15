import copy
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from xtrastock.config import Settings, REPO_ROOT
from xtrastock.deployment import render


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        shutil.copyfile(REPO_ROOT / 'config/defaults.toml', self.root / 'config/defaults.toml')

    def settings(self, **env):
        return Settings(self.root, env)

    def test_defaults_do_not_require_secrets_or_connect(self):
        settings = self.settings()
        self.assertEqual(settings.get('api.base_url'), 'https://api.example.invalid/v1')
        self.assertEqual(settings.get('database.port'), 5432)
        self.assertFalse((self.root / '.local').exists())

    def test_environment_dotenv_and_local_precedence(self):
        (self.root / 'config/local.toml').write_text('[database]\nport = 5433\nhost="config.invalid"\n')
        (self.root / '.env').write_text('FINANCE_PGPORT=5434\nFINANCE_PGHOST=file.invalid\n')
        settings = self.settings(FINANCE_PGPORT='5435')
        self.assertEqual(settings.get('database.port'), '5435')
        self.assertEqual(settings.get('database.host'), 'file.invalid')

    def test_external_files_are_explicit_and_missing_files_fail(self):
        with self.assertRaisesRegex(ValueError, 'XTRASTOCK_ENV_FILE'):
            self.settings(XTRASTOCK_ENV_FILE='missing.env')
        with self.assertRaisesRegex(ValueError, 'XTRASTOCK_CONFIG'):
            self.settings(XTRASTOCK_CONFIG='missing.toml')

    def test_quotes_and_equals_in_dotenv_secret(self):
        (self.root / '.env').write_text("APP_DB_PASSWORD='test=quoted#value'\n")
        self.assertEqual(self.settings().postgres_kwargs()['password'], 'test=quoted#value')

    def test_readonly_role_never_inherits_admin_override(self):
        settings = self.settings(FINANCE_PGUSER='writer', FINANCE_PGPASSWORD='test-writer',
                                 AI_READONLY_DB_PASSWORD='test-reader')
        self.assertEqual(settings.postgres_kwargs('reader')['user'], 'finance_ai_reader')
        self.assertEqual(settings.postgres_kwargs('reader')['password'], 'test-reader')
        self.assertEqual(settings.postgres_kwargs('app')['user'], 'writer')

    def test_missing_credentials_fail_before_network(self):
        for role in ('app', 'admin', 'reader'):
            with self.assertRaisesRegex(ValueError, f'Missing {role}'):
                self.settings().postgres_kwargs(role)

    def test_ssl_policy_and_libpq_aliases(self):
        result = self.settings(PGHOST='db.example.invalid', PGPORT='5544', PGSSLMODE='verify-full',
                               APP_DB_PASSWORD='test-password').postgres_kwargs()
        self.assertEqual((result['host'], result['port'], result['sslmode']), ('db.example.invalid', 5544, 'verify-full'))

    def test_invalid_port_and_dimensions_fail(self):
        for env in ({'FINANCE_PGPORT': '0'}, {'FINANCE_EMBEDDING_DIMENSIONS': '42'}):
            with self.assertRaises(ValueError):
                self.settings(**env)

    def test_legacy_consumers_receive_same_configuration(self):
        env = self.settings(FINANCE_API_KEY='test-api', FINAGENT_GATEWAY_KEY='test-gateway').legacy_environment()
        self.assertEqual(env['FINANCE_API_KEY'], env['FINAGENT_CLOUD_KEY'])
        self.assertEqual(env['FINANCE_OCR_API_KEY_ENV'], 'FINAGENT_GATEWAY_KEY')
        self.assertEqual(env['FINANCE_EMBEDDING_API_KEY_ENV'], 'FINAGENT_GATEWAY_KEY')
        self.assertEqual(env['FINANCE_CORPUS_ROOT'], str(self.root / 'apps/finagent'))

    def test_agent_overlay_does_not_mutate_persisted_workspace(self):
        source = {'engine_type': 'openai_compatible_xtrastock', 'engines': {}}
        before = copy.deepcopy(source)
        result = self.settings(FINANCE_API_KEY='test-api').agent_config(source)
        self.assertEqual(source, before)
        self.assertEqual(result['engines']['openai_compatible_xtrastock']['api_key'], 'test-api')
        self.assertEqual(result['engines']['openai_compatible_xtrastock']['model'], 'gpt-6-luna')

    def test_unrelated_engine_is_not_changed(self):
        source = {'engine_type': 'other', 'engines': {'other': {'model': 'custom'}}}
        self.assertEqual(self.settings().agent_config(source), source)

    def test_unit_render_resolves_tokens_without_actions(self):
        result = render('WorkingDirectory=@APP@\nExecStart=@VLLM_BIN@\n', self.settings())
        self.assertNotIn('@APP@', result)
        self.assertIn('/usr/local/bin/vllm', result)
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            render('@MISSING@', self.settings())

    def test_unsafe_render_paths_rejected(self):
        settings = self.settings()
        settings.values['deployment']['vllm_bin'] = '/tmp/invalid path/vllm'
        with self.assertRaises(ValueError):
            render('@VLLM_BIN@', settings)

    def test_psql_password_is_passed_only_in_child_environment(self):
        from xtrastock import psql
        settings = self.settings(POSTGRES_PASSWORD='test-admin-value')
        with patch.object(psql, 'Settings', return_value=settings), \
             patch.object(psql.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(psql.main(['--role', 'admin', '-c', 'SELECT 1']), 0)
        self.assertNotIn('test-admin-value', str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], 'test-admin-value')
        self.assertEqual(run.call_args.kwargs['env']['PGUSER'], 'finance_admin')


if __name__ == '__main__':
    unittest.main()
