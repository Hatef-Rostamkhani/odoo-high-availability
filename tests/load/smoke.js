import http from 'k6/http';
import { check, sleep } from 'k6';
export const options = { vus: 2, duration: '30s', thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<2000'] } };
export default function () {
  const response = http.get('http://127.0.0.1:8069/web/health?db_server_status=1');
  check(response, { 'healthy': r => r.status === 200 });
  sleep(1);
}
