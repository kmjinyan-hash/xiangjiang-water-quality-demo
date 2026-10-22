"""公开健康检查保留可选身份；登录后按站点权限返回状态。

独立运行：python tests/test_public_data_status.py
使用临时数据库，不修改当前用户和业务数据库。
"""
import importlib
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    with tempfile.TemporaryDirectory() as temporary:
        with patch.dict(os.environ, {
            'WQ_ROOT': str(root),
            'WQ_DATA_DIR': temporary,
            'WQ_LOGS_DIR': str(Path(temporary) / 'logs'),
            'WQ_ENABLE_SCHEDULER': '0',
            'WQ_REQUIRE_LOGIN': '1',
            'WQ_ADMIN_USER': 'status-test-admin',
            'WQ_ADMIN_PASSWORD': 'status-test-only-password',
        }):
            module = importlib.import_module('water_quality.app')
            client = module.app.test_client()
            expected = {sid.upper() for sid in module.TARGET_STATION_IDS}

            with patch.object(module.auth, 'current_user', return_value=(None, None)):
                response = client.get('/api/data_status')
                assert response.status_code == 200
                assert response.json['data'] == {}
                assert client.get('/api/predict').status_code == 401

            admin = {'id': 'test-admin', 'role': 'admin'}
            with patch.object(module.auth, 'current_user', return_value=(admin, {})):
                response = client.get('/api/data_status')
                assert response.status_code == 200
                assert set(response.json['data']) == expected

            viewer = {'id': 'test-viewer', 'role': 'viewer'}
            with patch.object(module.auth, 'current_user', return_value=(viewer, {})):
                with patch.object(module.permissions, 'get_user_stations', return_value=['st10']):
                    response = client.get('/api/data_status')
                    assert response.status_code == 200
                    assert set(response.json['data']) == {'ST10'}
                with patch.object(module.permissions, 'get_user_stations', return_value=[]):
                    assert client.get('/api/data_status').json['data'] == {}

            with patch.object(module.auth, 'current_user') as current_user:
                response = client.options('/api/data_status')
                assert response.status_code == 200
                current_user.assert_not_called()

    print('PASS: anonymous healthcheck, protected API, admin, scoped viewer, no grants, OPTIONS')


if __name__ == '__main__':
    main()
