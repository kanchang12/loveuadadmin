from flask import Flask, request, jsonify, session, redirect, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'loveuad-admin-secret-key-2025')

DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'LoveUAD2025!Admin')
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID')
GCP_BILLING_ACCOUNT = os.environ.get('GCP_BILLING_ACCOUNT')
BILLING_DATASET = os.environ.get('BILLING_DATASET')
CLOUD_RUN_SERVICE = os.environ.get('CLOUD_RUN_SERVICE', 'loveuad2')
CLOUD_RUN_REGION = os.environ.get('CLOUD_RUN_REGION', 'europe-west1')

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def check_auth():
    return session.get('admin') == True

# ==================== INIT TABLES ====================
def init_tables():
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Manual costs
        cur.execute("""CREATE TABLE IF NOT EXISTS manual_costs (
            id SERIAL PRIMARY KEY,
            cost_type VARCHAR(50) NOT NULL,
            amount DECIMAL(10,2) NOT NULL,
            month DATE NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # AI Compliance Audit Log
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_audit_log (
            id SERIAL PRIMARY KEY,
            code_hash VARCHAR(64) NOT NULL,
            action_type VARCHAR(50) NOT NULL,
            ai_output TEXT,
            user_action VARCHAR(50),
            context JSONB,
            model_version VARCHAR(50),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_ai_audit_timestamp ON ai_audit_log(timestamp)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_ai_audit_hash ON ai_audit_log(code_hash)""")
        
        conn.commit()
        conn.close()
        print("✓ Tables initialized (including AI compliance)")
    except Exception as e:
        print(f"Init tables error: {e}")

init_tables()

# ==================== CLOUD RUN LOGS (ERROR MONITORING) ====================
def fetch_cloud_run_errors():
    try:
        from google.cloud import logging as cloud_logging
        client = cloud_logging.Client(project=GCP_PROJECT_ID)
        
        # Filter for errors from Cloud Run service
        filter_str = f'''
        resource.type="cloud_run_revision"
        resource.labels.service_name="{CLOUD_RUN_SERVICE}"
        severity>=ERROR
        timestamp>="{(datetime.utcnow() - timedelta(days=7)).isoformat()}Z"
        '''
        
        entries = client.list_entries(filter_=filter_str, max_results=100, order_by=cloud_logging.DESCENDING)
        
        errors = []
        error_types = {}
        errors_24h = 0
        errors_7d = 0
        now = datetime.utcnow()
        
        for entry in entries:
            error_time = entry.timestamp.replace(tzinfo=None) if entry.timestamp else now
            age_hours = (now - error_time).total_seconds() / 3600
            
            error_type = 'Error'
            message = str(entry.payload) if entry.payload else 'No message'
            
            # Extract error type from message
            if 'TypeError' in message:
                error_type = 'TypeError'
            elif 'KeyError' in message:
                error_type = 'KeyError'
            elif 'ValueError' in message:
                error_type = 'ValueError'
            elif 'ConnectionError' in message:
                error_type = 'ConnectionError'
            elif 'TimeoutError' in message:
                error_type = 'TimeoutError'
            elif '500' in message:
                error_type = 'ServerError'
            elif '404' in message:
                error_type = 'NotFound'
            elif '503' in message:
                error_type = 'ServiceUnavailable'
            
            errors.append({
                'id': hash(str(entry.insert_id)),
                'error_type': error_type,
                'message': message[:200],
                'severity': str(entry.severity),
                'created_at': error_time.isoformat(),
                'resolved': False
            })
            
            error_types[error_type] = error_types.get(error_type, 0) + 1
            errors_7d += 1
            if age_hours <= 24:
                errors_24h += 1
        
        return {
            'errors_24h': errors_24h,
            'errors_7d': errors_7d,
            'unresolved': errors_7d,
            'recent': errors[:50],
            'by_type': [{'error_type': k, 'count': v} for k, v in sorted(error_types.items(), key=lambda x: -x[1])],
            'by_endpoint': [],
            'source': 'Cloud Run Logs'
        }
    except Exception as e:
        return {
            'errors_24h': 0,
            'errors_7d': 0,
            'unresolved': 0,
            'recent': [],
            'by_type': [],
            'by_endpoint': [],
            'error': str(e),
            'source': 'Error fetching logs'
        }

# ==================== TWILIO METRICS ====================
def fetch_twilio_metrics():
    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            return {'cost': 0, 'total_calls': 0, 'error': 'No Twilio credentials'}
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        start_date = datetime.utcnow() - timedelta(days=30)
        calls = client.calls.list(start_time_after=start_date, limit=1000)
        total_calls = len(calls)
        total_seconds = sum(int(call.duration or 0) for call in calls)
        total_minutes = total_seconds / 60
        cost = total_minutes * 0.013
        try:
            balance = float(client.api.accounts(TWILIO_ACCOUNT_SID).fetch().balance)
        except:
            balance = 0
        return {
            'total_calls': total_calls,
            'total_minutes': round(total_minutes, 2),
            'avg_duration': round(total_minutes / total_calls, 2) if total_calls > 0 else 0,
            'cost': round(cost, 2),
            'balance': balance
        }
    except Exception as e:
        return {'cost': 0, 'total_calls': 0, 'balance': 0, 'error': str(e)}

# ==================== GCP BILLING ====================
def fetch_gcp_billing():
    try:
        if not GCP_PROJECT_ID or not GCP_BILLING_ACCOUNT or not BILLING_DATASET:
            return {'total': 0, 'source': 'No GCP billing config'}
        from google.cloud import bigquery
        import google.auth
        credentials, project = google.auth.default()
        client = bigquery.Client(project=GCP_PROJECT_ID, credentials=credentials)
        billing_table = f"{BILLING_DATASET}.gcp_billing_export_v1_{GCP_BILLING_ACCOUNT.replace('-', '_')}"
        query = f"""
        SELECT service.description as service_name, SUM(cost) as total_cost
        FROM `{billing_table}`
        WHERE DATE(_PARTITIONTIME) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
        GROUP BY service.description ORDER BY total_cost DESC
        """
        results = client.query(query).result()
        costs = {}
        for row in results:
            costs[row.service_name] = float(row.total_cost)
        return {
            'cloud_run': round(costs.get('Cloud Run', 0), 2),
            'cloud_sql': round(costs.get('Cloud SQL', 0), 2),
            'networking': round(costs.get('Networking', 0), 2),
            'storage': round(costs.get('Cloud Storage', 0), 2),
            'total': round(sum(costs.values()), 2),
            'source': 'BigQuery (REAL)'
        }
    except Exception as e:
        return {'cloud_run': 0, 'cloud_sql': 0, 'total': 0, 'source': f'Error: {str(e)}'}

# ==================== DATABASE METRICS ====================
def fetch_database_metrics():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT pg_database_size(current_database()) as bytes")
        db_bytes = cur.fetchone()['bytes']
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
        active_conn = cur.fetchone()['count']
        cur.execute("""SELECT tablename, pg_total_relation_size('public.'||tablename) as bytes 
                       FROM pg_tables WHERE schemaname='public' ORDER BY bytes DESC LIMIT 10""")
        tables = cur.fetchall()
        conn.close()
        return {
            'size_gb': round(db_bytes / (1024**3), 2),
            'size_bytes': db_bytes,
            'active_connections': active_conn,
            'tables': [{'name': t['tablename'], 'size_mb': round(t['bytes']/(1024**2), 2)} for t in tables]
        }
    except Exception as e:
        return {'size_gb': 0, 'error': str(e)}

# ==================== GEMINI METRICS ====================
def fetch_gemini_metrics():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""SELECT query_type, SUM(input_tokens) as total_input, SUM(output_tokens) as total_output, COUNT(*) as count 
                       FROM gemini_usage WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days' GROUP BY query_type""")
        usage = cur.fetchall()
        conn.close()
        total_input, total_output, queries, scans = 0, 0, 0, 0
        for row in usage:
            total_input += row['total_input'] or 0
            total_output += row['total_output'] or 0
            if row['query_type'] == 'query': queries = row['count']
            elif row['query_type'] == 'scan': scans = row['count']
        input_cost = (total_input / 1_000_000) * 0.075
        output_cost = (total_output / 1_000_000) * 0.30
        return {
            'total_queries': queries,
            'total_scans': scans,
            'input_tokens': total_input,
            'output_tokens': total_output,
            'total_tokens': total_input + total_output,
            'cost': round(input_cost + output_cost, 2)
        }
    except Exception as e:
        return {'cost': 0, 'total_queries': 0, 'error': str(e)}

# ==================== USER METRICS ====================
def fetch_user_metrics():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM patients")
        total_users = cur.fetchone()['count']
        
        # Count users who OPENED app 1+ times in last 7 days
        cur.execute("SELECT COUNT(DISTINCT code_hash) as count FROM daily_launch_tracker WHERE launch_date >= CURRENT_DATE - INTERVAL '7 days'")
        active_once_7d = cur.fetchone()['count']
        
        # Count users who OPENED app 3+ times in last 7 days
        cur.execute("SELECT COUNT(*) as count FROM (SELECT code_hash FROM daily_launch_tracker WHERE launch_date >= CURRENT_DATE - INTERVAL '7 days' GROUP BY code_hash HAVING COUNT(*) >= 3) as t")
        active_thrice_7d = cur.fetchone()['count']
        
        cur.execute("SELECT DATE(created_at) as date, COUNT(*) as count FROM patients WHERE created_at >= CURRENT_DATE - INTERVAL '30 days' GROUP BY DATE(created_at) ORDER BY date DESC")
        daily_signups = cur.fetchall()
        recent_7d = sum(d['count'] for d in daily_signups[:7]) if len(daily_signups) >= 7 else sum(d['count'] for d in daily_signups)
        previous_7d = sum(d['count'] for d in daily_signups[7:14]) if len(daily_signups) >= 14 else 1
        growth_rate = ((recent_7d - previous_7d) / previous_7d * 100) if previous_7d > 0 else 0
        cur.execute("SELECT COUNT(DISTINCT t1.code_hash) as count FROM daily_launch_tracker t1 WHERE t1.launch_date >= CURRENT_DATE - INTERVAL '7 days' AND EXISTS (SELECT 1 FROM daily_launch_tracker t2 WHERE t2.code_hash = t1.code_hash AND t2.launch_date < CURRENT_DATE - INTERVAL '7 days')")
        retained = cur.fetchone()['count']
        retention_rate = (retained / active_once_7d * 100) if active_once_7d > 0 else 0
        conn.close()
        return {
            'total_users': total_users,
            'active_once_7d': active_once_7d,
            'active_thrice_7d': active_thrice_7d,
            'active_once_pct': round((active_once_7d / total_users * 100), 1) if total_users > 0 else 0,
            'active_thrice_pct': round((active_thrice_7d / total_users * 100), 1) if total_users > 0 else 0,
            'growth_rate': round(growth_rate, 1),
            'retention_rate': round(retention_rate, 1),
            'signups_7d': recent_7d,
            'signups_prev_7d': previous_7d,
            'daily_signups': [{'date': str(d['date']), 'count': d['count']} for d in daily_signups]
        }
    except Exception as e:
        return {'total_users': 0, 'error': str(e)}

# ==================== AI COMPLIANCE METRICS ====================
def fetch_ai_compliance_metrics():
    """Fetch AI audit trail statistics for compliance"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Total AI actions in last 30 days
        cur.execute("""
            SELECT COUNT(*) as total_actions
            FROM ai_audit_log
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
        """)
        total_actions = cur.fetchone()['total_actions']
        
        # Actions by type
        cur.execute("""
            SELECT action_type, COUNT(*) as count
            FROM ai_audit_log
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
            GROUP BY action_type
            ORDER BY count DESC
        """)
        by_type = cur.fetchall()
        
        # User acceptance rate
        cur.execute("""
            SELECT 
                COUNT(CASE WHEN user_action = 'accepted' THEN 1 END) as accepted,
                COUNT(CASE WHEN user_action = 'rejected' THEN 1 END) as rejected,
                COUNT(CASE WHEN user_action = 'modified' THEN 1 END) as modified,
                COUNT(*) as total
            FROM ai_audit_log
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
            AND user_action IS NOT NULL
        """)
        acceptance = cur.fetchone()
        
        acceptance_rate = (acceptance['accepted'] / acceptance['total'] * 100) if acceptance['total'] > 0 else 0
        
        # Recent AI actions (last 50)
        cur.execute("""
            SELECT 
                code_hash,
                action_type,
                user_action,
                model_version,
                timestamp
            FROM ai_audit_log
            ORDER BY timestamp DESC
            LIMIT 50
        """)
        recent = cur.fetchall()
        
        conn.close()
        
        return {
            'total_actions': total_actions,
            'by_type': [{'action_type': r['action_type'], 'count': r['count']} for r in by_type],
            'acceptance_rate': round(acceptance_rate, 1),
            'accepted': acceptance['accepted'],
            'rejected': acceptance['rejected'],
            'modified': acceptance['modified'],
            'recent': [dict(r) for r in recent]
        }
    except Exception as e:
        return {'total_actions': 0, 'error': str(e)}

# ==================== SATISFACTION ====================
def fetch_satisfaction_metrics():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT survey_day, result_bucket, COUNT(*) as count FROM survey_responses GROUP BY survey_day, result_bucket ORDER BY survey_day")
        survey_raw = cur.fetchall()
        conn.close()
        survey_by_day = {}
        for row in survey_raw:
            day = row['survey_day']
            if day not in survey_by_day:
                survey_by_day[day] = {'Low': 0, 'Medium': 0, 'High': 0, 'total': 0}
            survey_by_day[day][row['result_bucket']] = row['count']
            survey_by_day[day]['total'] += row['count']
        satisfaction_scores = {}
        for day, data in survey_by_day.items():
            if data['total'] > 0:
                satisfaction_scores[f"Day {day}"] = round((data['Low'] / data['total']) * 100, 1)
        return {'by_day': survey_by_day, 'scores': satisfaction_scores}
    except Exception as e:
        return {'scores': {}, 'error': str(e)}

# ==================== DAU ====================
def fetch_dau_metrics():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT event_date, SUM(launch_count) as count FROM daily_active_users WHERE event_date >= CURRENT_DATE - INTERVAL '30 days' GROUP BY event_date ORDER BY event_date DESC")
        dau_data = cur.fetchall()
        conn.close()
        return {'daily': [{'date': str(d['event_date']), 'count': d['count']} for d in dau_data]}
    except Exception as e:
        return {'daily': [], 'error': str(e)}

# ==================== MANUAL COSTS ====================
def fetch_manual_costs():
    try:
        conn = get_db()
        cur = conn.cursor()
        current_month = datetime.now().replace(day=1).date()
        cur.execute("SELECT cost_type, SUM(amount) as total FROM manual_costs WHERE month = %s GROUP BY cost_type", (current_month,))
        costs = cur.fetchall()
        conn.close()
        cost_dict = {c['cost_type']: float(c['total']) for c in costs}
        total = sum(cost_dict.values())
        return {
            'marketing': cost_dict.get('marketing', 0),
            'personnel': cost_dict.get('personnel', 0),
            'ads': cost_dict.get('ads', 0),
            'legal': cost_dict.get('legal', 0),
            'other': cost_dict.get('other', 0),
            'total': round(total, 2)
        }
    except Exception as e:
        return {'total': 0, 'error': str(e)}

# ==================== HEALTH CHECK ====================
def fetch_health_status():
    try:
        import requests
        checks = {}
        
        # DB check
        conn = get_db()
        cur = conn.cursor()
        start = datetime.now()
        cur.execute("SELECT 1")
        db_time = (datetime.now() - start).total_seconds() * 1000
        checks['database'] = {'status': 'ok', 'response_ms': round(db_time, 2)}
        
        cur.execute("SELECT COUNT(*) FROM patients")
        checks['patients_count'] = cur.fetchone()['count']
        cur.execute("SELECT COUNT(*) FROM medications")
        checks['medications_count'] = cur.fetchone()['count']
        conn.close()
        
        # Main app health check
        try:
            start = datetime.now()
            r = requests.get(f'https://{CLOUD_RUN_SERVICE}-{GCP_PROJECT_ID}.run.app/health', timeout=5)
            app_time = (datetime.now() - start).total_seconds() * 1000
            checks['main_app'] = {'status': 'ok' if r.status_code == 200 else 'error', 'response_ms': round(app_time, 2)}
        except:
            checks['main_app'] = {'status': 'unreachable', 'response_ms': 0}
        
        checks['overall'] = 'healthy' if checks['database']['status'] == 'ok' else 'unhealthy'
        return checks
    except Exception as e:
        return {'overall': 'unhealthy', 'error': str(e)}

# ==================== ROUTES ====================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template_string(LOGIN_HTML)
    data = request.json
    if data.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect('/login')

@app.route('/')
def dashboard():
    if not check_auth():
        return redirect('/login')
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/metrics')
def get_all_metrics():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        gcp_costs = fetch_gcp_billing()
        db_metrics = fetch_database_metrics()
        twilio_metrics = fetch_twilio_metrics()
        gemini_metrics = fetch_gemini_metrics()
        user_metrics = fetch_user_metrics()
        satisfaction_metrics = fetch_satisfaction_metrics()
        dau_metrics = fetch_dau_metrics()
        manual_costs = fetch_manual_costs()
        error_metrics = fetch_cloud_run_errors()
        health_status = fetch_health_status()
        ai_compliance = fetch_ai_compliance_metrics()
        
        automated_costs = gcp_costs.get('total', 0) + twilio_metrics.get('cost', 0) + gemini_metrics.get('cost', 0)
        total_costs = automated_costs + manual_costs.get('total', 0)
        total_users = user_metrics.get('total_users', 0)
        per_user_cost = total_costs / total_users if total_users > 0 else 0
        revenue_per_user = 5.0
        monthly_revenue = total_users * revenue_per_user
        profit_loss = monthly_revenue - total_costs
        breakeven_users = int(total_costs / revenue_per_user) if revenue_per_user > 0 else 0
        
        return jsonify({
            'success': True,
            'users': user_metrics,
            'costs': {
                'automated': {
                    'cloud_run': gcp_costs.get('cloud_run', 0),
                    'cloud_sql': gcp_costs.get('cloud_sql', 0),
                    'networking': gcp_costs.get('networking', 0),
                    'twilio': twilio_metrics.get('cost', 0),
                    'gemini': gemini_metrics.get('cost', 0),
                    'total': round(automated_costs, 2)
                },
                'manual': manual_costs,
                'total': round(total_costs, 2),
                'per_user': round(per_user_cost, 2)
            },
            'financial': {
                'total_costs': round(total_costs, 2),
                'monthly_revenue': round(monthly_revenue, 2),
                'profit_loss': round(profit_loss, 2),
                'breakeven_users': breakeven_users,
                'current_users': total_users
            },
            'details': {
                'gcp': gcp_costs,
                'database': db_metrics,
                'twilio': twilio_metrics,
                'gemini': gemini_metrics
            },
            'satisfaction': satisfaction_metrics,
            'dau': dau_metrics,
            'errors': error_metrics,
            'health': health_status,
            'ai_compliance': ai_compliance
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/manual-costs/add', methods=['POST'])
def add_manual_cost():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO manual_costs (cost_type, amount, month, notes) VALUES (%s, %s, %s, %s) RETURNING id",
                    (data.get('cost_type'), data.get('amount'), data.get('month', datetime.now().replace(day=1).date()), data.get('notes', '')))
        result = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'id': result['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/manual-costs/history')
def get_cost_history():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, cost_type, amount, month, notes, created_at FROM manual_costs ORDER BY month DESC, created_at DESC LIMIT 100")
        history = cur.fetchall()
        conn.close()
        return jsonify({'success': True, 'history': [dict(h) for h in history]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/compliance/ai-audit', methods=['GET'])
def get_ai_audit():
    """Get AI compliance audit trail"""
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Get query parameters
        code_hash = request.args.get('code_hash')
        limit = int(request.args.get('limit', 100))
        
        if code_hash:
            # Get audit trail for specific user
            cur.execute("""
                SELECT action_type, ai_output, user_action, context, model_version, timestamp
                FROM ai_audit_log
                WHERE code_hash = %s
                ORDER BY timestamp DESC
                LIMIT %s
            """, (code_hash, limit))
        else:
            # Get all recent audits
            cur.execute("""
                SELECT code_hash, action_type, user_action, model_version, timestamp
                FROM ai_audit_log
                ORDER BY timestamp DESC
                LIMIT %s
            """, (limit,))
        
        logs = cur.fetchall()
        conn.close()
        
        return jsonify({
            'success': True,
            'audit_trail': [dict(log) for log in logs]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== HTML TEMPLATES ====================
LOGIN_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin Login</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;display:flex;align-items:center;justify-content:center}.login-card{background:#fff;padding:40px;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);width:100%;max-width:400px}h1{color:#333;margin-bottom:30px;text-align:center}input{width:100%;padding:15px;border:2px solid #ddd;border-radius:8px;font-size:16px;margin-bottom:20px}input:focus{outline:none;border-color:#667eea}button{width:100%;padding:15px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer}button:hover{opacity:0.9}.error{background:#fee;color:#c00;padding:10px;border-radius:6px;margin-bottom:20px;display:none}</style></head>
<body><div class="login-card"><h1>🔒 Admin Login</h1><div class="error" id="error"></div><input type="password" id="password" placeholder="Enter admin password"/><button onclick="login()">Login</button></div>
<script>document.getElementById('password').addEventListener('keypress',function(e){if(e.key==='Enter')login()});async function login(){const password=document.getElementById('password').value;const errorDiv=document.getElementById('error');try{const response=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});const data=await response.json();if(data.success){window.location.href='/'}else{errorDiv.textContent='Invalid password';errorDiv.style.display='block'}}catch(error){errorDiv.textContent='Login failed';errorDiv.style.display='block'}}</script></body></html>'''

DASHBOARD_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#fff}.header{background:#111;padding:20px;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center}.header h1{font-size:1.8rem;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.logout-btn{padding:10px 20px;background:#f87171;color:#fff;border:none;border-radius:6px;cursor:pointer;text-decoration:none}.tabs{display:flex;background:#111;border-bottom:1px solid #333;flex-wrap:wrap}.tab{padding:15px 25px;cursor:pointer;border-bottom:3px solid transparent}.tab.active{border-bottom-color:#667eea;background:#1a1a1a}.content{padding:30px;max-width:1600px;margin:0 auto}.tab-content{display:none}.tab-content.active{display:block}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;margin-bottom:30px}.card{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:20px}.card h3{color:#888;font-size:0.85rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}.card .value{font-size:2.5rem;font-weight:bold;margin-bottom:5px}.card .subvalue{color:#888;font-size:0.9rem}.positive{color:#4ade80}.negative{color:#f87171}.neutral{color:#fbbf24}.section{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:25px;margin-bottom:30px}.section h2{margin-bottom:20px;font-size:1.5rem}table{width:100%;border-collapse:collapse}table th,table td{padding:12px;text-align:left;border-bottom:1px solid #333}table th{color:#888;font-weight:600;text-transform:uppercase;font-size:0.85rem}.form-group{margin-bottom:20px}.form-group label{display:block;color:#888;margin-bottom:8px;font-weight:600}.form-group input,.form-group select,.form-group textarea{width:100%;padding:12px;background:#0a0a0a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px}.btn{padding:12px 24px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer}.loading{text-align:center;padding:40px;color:#888}.chart-container{position:relative;height:300px;margin-top:20px}.source-badge{display:inline-block;padding:4px 8px;background:#667eea;color:#fff;border-radius:4px;font-size:0.75rem;margin-left:10px}.health-ok{color:#4ade80}.health-bad{color:#f87171}.badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}.badge-error{background:#f87171;color:#fff}.badge-warning{background:#fbbf24;color:#000}.compliance-badge{background:#10b981;color:#fff;padding:4px 10px;border-radius:6px;font-size:0.85rem;font-weight:600}</style></head>
<body><div class="header"><h1>📊 loveUAD Admin Dashboard</h1><div><span id="health-indicator">⏳</span> <a href="/logout" class="logout-btn">Logout</a></div></div>
<div class="tabs"><div class="tab active" onclick="switchTab(event,'financial')">💰 Financial</div><div class="tab" onclick="switchTab(event,'accounting')">📝 Accounting</div><div class="tab" onclick="switchTab(event,'customers')">👥 Customers</div><div class="tab" onclick="switchTab(event,'errors')">🚨 Errors</div><div class="tab" onclick="switchTab(event,'health')">💓 Health</div></div>
<div class="content"><div class="loading" id="loading">Loading data...</div>
<div id="financial-tab" class="tab-content active"><div class="grid"><div class="card"><h3>Total Monthly Cost</h3><div class="value negative">$<span id="fin-total-cost">-</span></div><div class="subvalue">Automated + Manual</div></div><div class="card"><h3>Monthly Revenue</h3><div class="value positive">$<span id="fin-revenue">-</span></div><div class="subvalue">$5 × <span id="fin-users">-</span> users</div></div><div class="card"><h3>Profit / Loss</h3><div class="value" id="fin-profit">$-</div><div class="subvalue">Revenue - Costs</div></div><div class="card"><h3>Per User Cost</h3><div class="value neutral">$<span id="fin-per-user">-</span></div><div class="subvalue">Total ÷ users</div></div><div class="card"><h3>Breakeven Point</h3><div class="value neutral"><span id="fin-breakeven">-</span></div><div class="subvalue">Users needed</div></div></div><div class="section"><h2>💸 Cost Breakdown <span class="source-badge" id="gcp-source">Loading...</span></h2><table><tr><th>Category</th><th>Amount</th></tr><tr><td>☁️ Cloud Run</td><td>$<span id="cost-cloudrun">-</span></td></tr><tr><td>🗄️ Cloud SQL</td><td>$<span id="cost-cloudsql">-</span></td></tr><tr><td>🌐 Networking</td><td>$<span id="cost-networking">-</span></td></tr><tr><td>📞 Twilio Calls</td><td>$<span id="cost-twilio">-</span></td></tr><tr><td>🤖 Gemini API</td><td>$<span id="cost-gemini">-</span></td></tr><tr style="border-top:2px solid #667eea"><td><strong>Automated Total</strong></td><td><strong>$<span id="cost-auto-total">-</span></strong></td></tr><tr><td>📢 Marketing</td><td>$<span id="cost-marketing">-</span></td></tr><tr><td>👨‍💼 Personnel</td><td>$<span id="cost-personnel">-</span></td></tr><tr><td>📺 Advertisements</td><td>$<span id="cost-ads">-</span></td></tr><tr><td>⚖️ Legal</td><td>$<span id="cost-legal">-</span></td></tr><tr><td>🔧 Other</td><td>$<span id="cost-other">-</span></td></tr><tr style="border-top:2px solid #667eea"><td><strong>Manual Total</strong></td><td><strong>$<span id="cost-manual-total">-</span></strong></td></tr><tr style="border-top:3px solid #f87171"><td><strong>GRAND TOTAL</strong></td><td><strong>$<span id="cost-grand-total">-</span></strong></td></tr></table></div><div class="section"><h2>🤖 AI Compliance <span class="compliance-badge">HIPAA/GDPR/MHRA</span></h2><table><tr><td>Total AI Actions (30d)</td><td><strong><span id="ai-total-actions">-</span></strong></td></tr><tr><td>User Acceptance Rate</td><td><strong><span id="ai-acceptance-rate">-</span>%</strong></td></tr><tr><td>✅ Accepted</td><td><strong><span id="ai-accepted">-</span></strong></td></tr><tr><td>❌ Rejected</td><td><strong><span id="ai-rejected">-</span></strong></td></tr><tr><td>✏️ Modified</td><td><strong><span id="ai-modified">-</span></strong></td></tr></table></div><div class="section"><h2>📈 Financial Projection</h2><div class="chart-container"><canvas id="financial-chart"></canvas></div></div></div>
<div id="accounting-tab" class="tab-content"><div class="section"><h2>➕ Add Manual Cost</h2><div class="form-group"><label>Cost Type</label><select id="cost-type"><option value="marketing">Marketing</option><option value="personnel">Personnel</option><option value="ads">Advertisements</option><option value="legal">Legal</option><option value="other">Other</option></select></div><div class="form-group"><label>Amount ($)</label><input type="number" id="cost-amount" step="0.01" placeholder="0.00"/></div><div class="form-group"><label>Month</label><input type="month" id="cost-month"/></div><div class="form-group"><label>Notes</label><textarea id="cost-notes" rows="3" placeholder="Add notes..."></textarea></div><button class="btn" onclick="addCost()">Add Cost</button></div><div class="section"><h2>📜 Cost History</h2><table><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Notes</th></tr></thead><tbody id="cost-history-body"><tr><td colspan="4" style="text-align:center;color:#888">Loading...</td></tr></tbody></table></div></div>
<div id="customers-tab" class="tab-content"><div class="grid"><div class="card"><h3>Total Users</h3><div class="value"><span id="cust-total">-</span></div></div><div class="card"><h3>Active (1+ open, 7d)</h3><div class="value"><span id="cust-active-once">-</span></div><div class="subvalue"><span id="cust-active-once-pct">-</span>%</div></div><div class="card"><h3>Active (3+ opens, 7d)</h3><div class="value"><span id="cust-active-thrice">-</span></div><div class="subvalue"><span id="cust-active-thrice-pct">-</span>%</div></div><div class="card"><h3>Growth Rate (7d)</h3><div class="value" id="cust-growth">-</div><div class="subvalue"><span id="cust-signups-7d">-</span> new</div></div><div class="card"><h3>Retention (7d)</h3><div class="value" id="cust-retention">-</div></div></div><div class="section"><h2>📊 Database</h2><table><tr><td>Size</td><td><strong><span id="db-size">-</span> GB</strong></td></tr><tr><td>Active Connections</td><td><strong><span id="db-connections">-</span></strong></td></tr></table></div><div class="section"><h2>📞 Twilio</h2><table><tr><td>Total Calls (30d)</td><td><strong><span id="twilio-calls">-</span></strong></td></tr><tr><td>Total Duration</td><td><strong><span id="twilio-duration">-</span> min</strong></td></tr><tr><td>Avg Duration</td><td><strong><span id="twilio-avg">-</span> min</strong></td></tr><tr><td>Balance</td><td><strong>$<span id="twilio-balance">-</span></strong></td></tr></table></div><div class="section"><h2>🤖 AI Usage</h2><table><tr><td>Queries</td><td><strong><span id="gemini-queries">-</span></strong></td></tr><tr><td>Scans</td><td><strong><span id="gemini-scans">-</span></strong></td></tr><tr><td>Total Tokens</td><td><strong><span id="gemini-tokens">-</span></strong></td></tr></table></div><div class="section"><h2>📈 DAU (30d)</h2><div class="chart-container"><canvas id="dau-chart"></canvas></div></div></div>
<div id="errors-tab" class="tab-content"><div class="grid"><div class="card"><h3>Errors (24h)</h3><div class="value negative" id="err-24h">-</div></div><div class="card"><h3>Errors (7d)</h3><div class="value negative" id="err-7d">-</div></div><div class="card"><h3>Unresolved</h3><div class="value neutral" id="err-unresolved">-</div></div></div><div class="section"><h2>📊 Errors by Type <span class="source-badge" id="err-source">Cloud Run Logs</span></h2><table><thead><tr><th>Error Type</th><th>Count</th></tr></thead><tbody id="err-by-type"><tr><td colspan="2" style="text-align:center;color:#888">Loading...</td></tr></tbody></table></div><div class="section"><h2>🚨 Recent Errors</h2><table><thead><tr><th>Time</th><th>Type</th><th>Message</th><th>Severity</th></tr></thead><tbody id="err-recent"><tr><td colspan="4" style="text-align:center;color:#888">Loading...</td></tr></tbody></table></div></div>
<div id="health-tab" class="tab-content"><div class="grid"><div class="card"><h3>Overall Status</h3><div class="value" id="health-overall">-</div></div><div class="card"><h3>Database</h3><div class="value" id="health-db">-</div><div class="subvalue" id="health-db-ms">-</div></div><div class="card"><h3>Patients</h3><div class="value" id="health-patients">-</div></div><div class="card"><h3>Medications</h3><div class="value" id="health-meds">-</div></div></div><div class="section"><h2>🔄 Auto-Refresh</h2><p>Dashboard refreshes every 30 seconds. Last updated: <span id="last-update">-</span></p></div></div></div>
<script>let chartInstances={};function switchTab(e,t){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));e.target.classList.add('active');document.getElementById(t+'-tab').classList.add('active')}async function loadMetrics(){try{const r=await fetch('/api/metrics');const d=await r.json();if(d.success){updateFinancialTab(d);updateCustomerTab(d);updateErrorsTab(d);updateHealthTab(d);loadCostHistory();document.getElementById('loading').style.display='none';document.getElementById('last-update').textContent=new Date().toLocaleTimeString()}}catch(e){document.getElementById('loading').innerHTML='Error: '+e.message}}function updateFinancialTab(d){const f=d.financial||{};const c=d.costs||{};const a=c.automated||{};const m=c.manual||{};const ai=d.ai_compliance||{};document.getElementById('fin-total-cost').textContent=(f.total_costs||0).toFixed(2);document.getElementById('fin-revenue').textContent=(f.monthly_revenue||0).toFixed(2);document.getElementById('fin-users').textContent=f.current_users||0;const p=document.getElementById('fin-profit');p.textContent='$'+(f.profit_loss||0).toFixed(2);p.className='value '+((f.profit_loss||0)>=0?'positive':'negative');document.getElementById('fin-per-user').textContent=(c.per_user||0).toFixed(2);document.getElementById('fin-breakeven').textContent=f.breakeven_users||0;document.getElementById('cost-cloudrun').textContent=(a.cloud_run||0).toFixed(2);document.getElementById('cost-cloudsql').textContent=(a.cloud_sql||0).toFixed(2);document.getElementById('cost-networking').textContent=(a.networking||0).toFixed(2);document.getElementById('cost-twilio').textContent=(a.twilio||0).toFixed(2);document.getElementById('cost-gemini').textContent=(a.gemini||0).toFixed(2);document.getElementById('cost-auto-total').textContent=(a.total||0).toFixed(2);document.getElementById('cost-marketing').textContent=(m.marketing||0).toFixed(2);document.getElementById('cost-personnel').textContent=(m.personnel||0).toFixed(2);document.getElementById('cost-ads').textContent=(m.ads||0).toFixed(2);document.getElementById('cost-legal').textContent=(m.legal||0).toFixed(2);document.getElementById('cost-other').textContent=(m.other||0).toFixed(2);document.getElementById('cost-manual-total').textContent=(m.total||0).toFixed(2);document.getElementById('cost-grand-total').textContent=(c.total||0).toFixed(2);document.getElementById('gcp-source').textContent=(d.details&&d.details.gcp&&d.details.gcp.source)||'Loading...';document.getElementById('ai-total-actions').textContent=ai.total_actions||0;document.getElementById('ai-acceptance-rate').textContent=ai.acceptance_rate||0;document.getElementById('ai-accepted').textContent=ai.accepted||0;document.getElementById('ai-rejected').textContent=ai.rejected||0;document.getElementById('ai-modified').textContent=ai.modified||0;if(chartInstances.financial)chartInstances.financial.destroy();chartInstances.financial=new Chart(document.getElementById('financial-chart').getContext('2d'),{type:'bar',data:{labels:['Costs','Revenue','Profit/Loss'],datasets:[{data:[f.total_costs||0,f.monthly_revenue||0,f.profit_loss||0],backgroundColor:['#f87171','#4ade80',(f.profit_loss||0)>=0?'#4ade80':'#f87171']}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,ticks:{color:'#888'},grid:{color:'#333'}},x:{ticks:{color:'#888'},grid:{color:'#333'}}}}})}function updateCustomerTab(d){const u=d.users||{};const db=(d.details&&d.details.database)||{};const tw=(d.details&&d.details.twilio)||{};const gm=(d.details&&d.details.gemini)||{};document.getElementById('cust-total').textContent=u.total_users||0;document.getElementById('cust-active-once').textContent=u.active_once_7d||0;document.getElementById('cust-active-once-pct').textContent=u.active_once_pct||0;document.getElementById('cust-active-thrice').textContent=u.active_thrice_7d||0;document.getElementById('cust-active-thrice-pct').textContent=u.active_thrice_pct||0;const g=document.getElementById('cust-growth');g.textContent=(u.growth_rate||0)+'%';g.className='value '+((u.growth_rate||0)>=0?'positive':'negative');document.getElementById('cust-signups-7d').textContent=u.signups_7d||0;const r=document.getElementById('cust-retention');r.textContent=(u.retention_rate||0)+'%';r.className='value '+((u.retention_rate||0)>=50?'positive':'neutral');document.getElementById('db-size').textContent=db.size_gb||0;document.getElementById('db-connections').textContent=db.active_connections||0;document.getElementById('twilio-calls').textContent=tw.total_calls||0;document.getElementById('twilio-duration').textContent=tw.total_minutes||0;document.getElementById('twilio-avg').textContent=tw.avg_duration||0;document.getElementById('twilio-balance').textContent=tw.balance||0;document.getElementById('gemini-queries').textContent=gm.total_queries||0;document.getElementById('gemini-scans').textContent=gm.total_scans||0;document.getElementById('gemini-tokens').textContent=(gm.total_tokens||0).toLocaleString();const dau=(d.dau&&d.dau.daily)||[];if(dau.length>0){if(chartInstances.dau)chartInstances.dau.destroy();chartInstances.dau=new Chart(document.getElementById('dau-chart').getContext('2d'),{type:'line',data:{labels:dau.map(d=>d.date).reverse(),datasets:[{label:'DAU',data:dau.map(d=>d.count).reverse(),borderColor:'#667eea',backgroundColor:'rgba(102,126,234,0.1)',fill:true,tension:0.4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,ticks:{color:'#888'},grid:{color:'#333'}},x:{ticks:{color:'#888',maxRotation:45,minRotation:45},grid:{color:'#333'}}}}})}}function updateErrorsTab(d){const e=d.errors||{};document.getElementById('err-24h').textContent=e.errors_24h||0;document.getElementById('err-7d').textContent=e.errors_7d||0;document.getElementById('err-unresolved').textContent=e.unresolved||0;if(e.source)document.getElementById('err-source').textContent=e.source;const hi=document.getElementById('health-indicator');if((e.errors_24h||0)>0){hi.textContent='🔴 '+e.errors_24h+' errors (24h)';hi.className='health-bad'}else{hi.textContent='🟢 Healthy';hi.className='health-ok'}const byType=document.getElementById('err-by-type');byType.innerHTML='';if(e.by_type&&e.by_type.length>0){e.by_type.forEach(t=>{const row=byType.insertRow();row.insertCell(0).textContent=t.error_type;row.insertCell(1).textContent=t.count})}else{byType.innerHTML='<tr><td colspan="2" style="text-align:center;color:#888">No errors 🎉</td></tr>'}const recent=document.getElementById('err-recent');recent.innerHTML='';if(e.recent&&e.recent.length>0){e.recent.forEach(err=>{const row=recent.insertRow();row.insertCell(0).textContent=new Date(err.created_at).toLocaleString();row.insertCell(1).innerHTML='<span class="badge badge-error">'+err.error_type+'</span>';row.insertCell(2).textContent=err.message||'-';row.insertCell(3).textContent=err.severity||'ERROR'})}else{recent.innerHTML='<tr><td colspan="4" style="text-align:center;color:#888">No errors 🎉</td></tr>'}}function updateHealthTab(d){const h=d.health||{};const overall=document.getElementById('health-overall');overall.textContent=h.overall||'unknown';overall.className='value '+(h.overall==='healthy'?'positive':'negative');const dbStatus=document.getElementById('health-db');const dbMs=document.getElementById('health-db-ms');if(h.database){dbStatus.textContent=h.database.status;dbStatus.className='value '+(h.database.status==='ok'?'positive':'negative');dbMs.textContent=(h.database.response_ms||0)+'ms'}document.getElementById('health-patients').textContent=h.patients_count||0;document.getElementById('health-meds').textContent=h.medications_count||0}async function addCost(){const t=document.getElementById('cost-type').value;const a=parseFloat(document.getElementById('cost-amount').value);const m=document.getElementById('cost-month').value;const n=document.getElementById('cost-notes').value;if(!a||a<=0){alert('Enter valid amount');return}try{const r=await fetch('/api/manual-costs/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cost_type:t,amount:a,month:m,notes:n})});const d=await r.json();if(d.success){alert('Cost added');document.getElementById('cost-amount').value='';document.getElementById('cost-notes').value='';loadMetrics();loadCostHistory()}}catch(e){alert('Error adding cost')}}async function loadCostHistory(){try{const r=await fetch('/api/manual-costs/history');const d=await r.json();const tb=document.getElementById('cost-history-body');tb.innerHTML='';if(d.history&&d.history.length>0){d.history.forEach(c=>{const row=tb.insertRow();row.insertCell(0).textContent=new Date(c.created_at).toLocaleDateString();row.insertCell(1).textContent=c.cost_type;row.insertCell(2).textContent='$'+parseFloat(c.amount).toFixed(2);row.insertCell(3).textContent=c.notes||'-'})}else{tb.innerHTML='<tr><td colspan="4" style="text-align:center;color:#888">No entries</td></tr>'}}catch(e){console.error('History error:',e)}}document.getElementById('cost-month').value=new Date().toISOString().slice(0,7);loadMetrics();setInterval(loadMetrics,30000)</script></body></html>'''

if __name__ == '__main__':
    app.run(debug=True, port=5000)
