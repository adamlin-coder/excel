import csv
import io
import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from app import main


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        main.DATA = main.Path(cls.temp.name)
        os.environ['CSV_USERS_JSON'] = main.json.dumps({'alice': main.password_hash('test-password'), 'bob': main.password_hash('bob-password')})
        os.environ['CSV_SESSION_SECRET'] = 'test-session-key-' * 4
        os.environ['CSV_SECURE_COOKIE'] = '0'
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        response = cls.client.post('/api/login', json={'username': 'alice', 'password': 'test-password'})
        assert response.status_code == 200

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.temp.cleanup()

    def upload(self, content, **params):
        response = self.client.post('/api/datasets', params=params, files={'file': ('資料.csv', content, 'text/csv')})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['id']

    def test_authentication_required_and_invalid_session(self):
        with TestClient(main.app) as guest:
            self.assertEqual(guest.get('/api/datasets').status_code, 401)
            guest.cookies.set('csv_session', 'invalid.cookie')
            self.assertEqual(guest.get('/api/datasets').status_code, 401)
            self.assertEqual(guest.post('/api/login', json={'username': 'alice', 'password': 'wrong'}).status_code, 401)

    def test_filter_edit_conflict_audit_and_export(self):
        dataset = self.upload('名稱,代碼,備註\n甲,001,"第一行\n第二行"\n乙,002,100%完成\n丙,003,\n'.encode())
        path = f'/api/datasets/{dataset}'
        result = self.client.get(path + '/rows').json()
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['rows'][0]['values'], ['甲', '001', '第一行\n第二行'])
        filtered = self.client.get(path + '/rows', params={'q': '%'}).json()
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(self.client.get(path + '/rows', params={'column': 2, 'op': 'empty'}).json()['total'], 1)
        self.assertEqual(self.client.get(path + '/rows', params={'sort': 1, 'direction': 'desc', 'offset': 1, 'limit': 1}).json()['rows'][0]['values'][1], '002')
        change = {'column': 0, 'value': '已更新', 'version': 1}
        self.assertEqual(self.client.patch(path + '/rows/1', json=change).status_code, 200)
        # A second user's stale version cannot overwrite the first user's edit.
        with TestClient(main.app) as bob:
            bob.post('/api/login', json={'username': 'bob', 'password': 'bob-password'})
            self.assertEqual(bob.patch(path + '/rows/1', json=change).status_code, 409)
            self.assertEqual(bob.get(path + '/rows').json()['rows'][0]['values'][0], '已更新')
        audit = self.client.get(path + '/audit').json()
        self.assertEqual(audit[0]['username'], 'alice')
        self.assertEqual(audit[0]['old_value'], '甲')
        export = self.client.get(path + '/export', params={'column': 1, 'op': 'equals', 'value': '001'})
        data = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        self.assertEqual(data, [['名稱', '代碼', '備註'], ['已更新', '001', '第一行\n第二行']])
        # Undo is a normal version-checked edit, recorded in audit too.
        self.assertEqual(self.client.patch(path + '/rows/1', json={'column': 0, 'value': '甲', 'version': 2}).status_code, 200)
        self.assertEqual(len(self.client.get(path + '/audit').json()), 2)

    def test_cp950_and_duplicate_headers(self):
        dataset = self.upload('名稱;名稱\n中文;0001\n'.encode('cp950'), encoding='cp950', delimiter=';')
        result = self.client.get(f'/api/datasets/{dataset}/rows').json()
        self.assertEqual(result['columns'], ['名稱', '名稱'])
        self.assertEqual(result['rows'][0]['values'], ['中文', '0001'])

    def test_malformed_file_is_failed_not_partially_ready(self):
        dataset = self.upload(b'a,b\n1,2\n3,4,5\n', delimiter=',')
        info = next(x for x in self.client.get('/api/datasets').json() if x['id'] == dataset)
        self.assertEqual(info['state'], 'failed')
        self.assertEqual(self.client.get(f'/api/datasets/{dataset}/rows').status_code, 409)
        self.assertFalse((main.DATA / f'{dataset}.upload').exists())

    def test_validation_and_upload_limit(self):
        dataset = self.upload(b'a,b\n1,2\n')
        base = f'/api/datasets/{dataset}'
        self.assertEqual(self.client.get(base + '/rows', params={'column': 20}).status_code, 400)
        self.assertEqual(self.client.get(base + '/rows', params={'direction': 'drop table'}).status_code, 400)
        self.assertEqual(self.client.get('/api/datasets/not-a-uuid/rows').status_code, 404)
        self.assertEqual(self.client.get(base + '/rows', params={'limit': 100000}).status_code, 400)
        before = set(main.DATA.glob('*.upload'))
        old_limit = main.MAX_BYTES
        try:
            main.MAX_BYTES = 5
            response = self.client.post('/api/datasets', files={'file': ('large.csv', b'a\n123456\n')})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(before, set(main.DATA.glob('*.upload')))
        finally:
            main.MAX_BYTES = old_limit


if __name__ == '__main__':
    unittest.main()
