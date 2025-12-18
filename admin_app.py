from flask import Flask, render_template_string, request, jsonify, session, redirect
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Response
import os
from datetime import datetime, timedelta
from functools import wraps
import re

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('ADMIN_SECRET_KEY', 'change-this-in-production')


# Config
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', '')
GCP_BILLING_ACCOUNT = os.environ.get('GCP_BILLING_ACCOUNT', '')
BILLING_DATASET = os.environ.get('BILLING_DATASET', '')
CLOUD_RUN_SERVICE = os.environ.get('CLOUD_RUN_SERVICE', 'loveuad')
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def check_auth():
    return session.get('admin', False)

def generate_slug(title):
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')

# ==================== TABLE INITIALIZATION ====================
def init_tables():
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Manual costs table
        cur.execute("""CREATE TABLE IF NOT EXISTS manual_costs (
            id SERIAL PRIMARY KEY,
            cost_type VARCHAR(50) NOT NULL,
            amount DECIMAL(10,2) NOT NULL,
            month DATE NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # AI audit log table
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
        
        # Blog posts table
        cur.execute("""CREATE TABLE IF NOT EXISTS blog_posts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            slug VARCHAR(255) UNIQUE NOT NULL,
            content TEXT NOT NULL,
            excerpt TEXT,
            meta_description VARCHAR(160),
            keywords TEXT,
            author VARCHAR(100) DEFAULT 'Kanchan Ghosh',
            featured_image VARCHAR(500),
            status VARCHAR(20) DEFAULT 'draft',
            published_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_blog_slug ON blog_posts(slug)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_blog_status ON blog_posts(status)""")

        cur.execute("""CREATE TABLE IF NOT EXISTS blog_comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER REFERENCES blog_posts(id) ON DELETE CASCADE,
            author_name VARCHAR(100) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        

        
        conn.commit()
        conn.close()
        print("✓ Tables initialized")
    except Exception as e:
        print(f"Init tables error: {e}")



init_tables()

# ==================== DELETION REQUESTS ====================
def fetch_deletion_requests():
    """Fetch pending account deletion requests"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                patient_code, 
                requested_at,
                EXTRACT(DAY FROM (CURRENT_TIMESTAMP - requested_at)) as days_pending
            FROM deletion_requests 
            WHERE status = 'pending'
            ORDER BY requested_at DESC
        """)
        requests = cur.fetchall()
        conn.close()
        return [dict(r) for r in requests]
    except Exception as e:
        print(f"Deletion requests error: {e}")
        return []

@app.route('/blog/rss')
def blog_rss():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT title, slug, excerpt, published_at 
            FROM blog_posts 
            WHERE status = 'published' 
            ORDER BY published_at DESC LIMIT 20
        """)
        posts = cur.fetchall()
        conn.close()

        rss_xml = '<?xml version="1.0" encoding="UTF-8" ?>\n'
        rss_xml += '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
        rss_xml += '  <title>loveUAD Blog</title>\n'
        rss_xml += '  <link>https://blog.loveuad.com/blog</link>\n'
        rss_xml += '  <description>Latest insights on dementia care and health technology</description>\n'
        
        for post in posts:
            pub_date = post['published_at'].strftime('%a, %d %b %Y %H:%M:%S GMT')
            rss_xml += f'''  <item>
    <title>{post['title']}</title>
    <link>https://blog.loveuad.com/blog/{post['slug']}</link>
    <description>{post['excerpt']}</description>
    <pubDate>{pub_date}</pubDate>
    <guid>https://blog.loveuad.com/blog/{post['slug']}</guid>
  </item>\n'''
        
        rss_xml += '</channel>\n</rss>'
        return Response(rss_xml, mimetype='application/rss+xml')
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/api/blog/posts/<int:post_id>/comments', methods=['GET'])
def get_comments(post_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT author_name, content, created_at FROM blog_comments WHERE post_id = %s ORDER BY created_at DESC", (post_id,))
        comments = cur.fetchall()
        conn.close()
        return jsonify({'success': True, 'comments': [dict(c) for c in comments]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/blog/posts/<int:post_id>/comments', methods=['POST'])
def add_comment(post_id):
    try:
        data = request.json
        name = data.get('name', 'Anonymous').strip()
        content = data.get('content', '').strip()
        if not content:
            return jsonify({'error': 'Comment content required'}), 400
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO blog_comments (post_id, author_name, content) VALUES (%s, %s, %s)", (post_id, name, content))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== CLOUD RUN LOGS (ERROR MONITORING) ====================
def fetch_cloud_run_errors():
    try:
        from google.cloud import logging as cloud_logging
        client = cloud_logging.Client(project=GCP_PROJECT_ID)
        
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
        
        cur.execute("SELECT COUNT(DISTINCT code_hash) as count FROM daily_launch_tracker WHERE launch_date >= CURRENT_DATE - INTERVAL '7 days'")
        active_once_7d = cur.fetchone()['count']
        
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
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT COUNT(*) as total_actions
            FROM ai_audit_log
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
        """)
        total_actions = cur.fetchone()['total_actions']
        
        cur.execute("""
            SELECT action_type, COUNT(*) as count
            FROM ai_audit_log
            WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
            GROUP BY action_type
            ORDER BY count DESC
        """)
        by_type = cur.fetchall()
        
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

# ==================== BLOG API ROUTES ====================
@app.route('/api/blog/posts', methods=['GET'])
def get_blog_posts():
    try:
        conn = get_db()
        cur = conn.cursor()
        if check_auth():
            cur.execute("SELECT id, title, slug, excerpt, author, status, published_at, created_at FROM blog_posts ORDER BY created_at DESC")
        else:
            cur.execute("SELECT id, title, slug, excerpt, author, published_at FROM blog_posts WHERE status = 'published' ORDER BY published_at DESC")
        posts = cur.fetchall()
        conn.close()
        return jsonify({'success': True, 'posts': [dict(p) for p in posts]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/blog/posts/<int:post_id>', methods=['GET'])
def get_blog_post(post_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM blog_posts WHERE id = %s", (post_id,))
        post = cur.fetchone()
        conn.close()
        if not post:
            return jsonify({'error': 'Post not found'}), 404
        if post['status'] != 'published' and not check_auth():
            return jsonify({'error': 'Unauthorized'}), 403
        return jsonify({'success': True, 'post': dict(post)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/blog/posts', methods=['POST'])
def create_blog_post():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        title = data.get('title', '').strip()
        content = data.get('content', '').strip()
        if not title or not content:
            return jsonify({'error': 'Title and content required'}), 400
        slug = generate_slug(title)
        excerpt = data.get('excerpt') or content[:200]
        meta_description = data.get('meta_description') or content[:160]
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM blog_posts WHERE slug = %s", (slug,))
        if cur.fetchone():
            slug = f"{slug}-{int(datetime.now().timestamp())}"
        status = data.get('status', 'draft')
        published_at = datetime.now() if status == 'published' else None
        cur.execute("""INSERT INTO blog_posts (title, slug, content, excerpt, meta_description, keywords, author, featured_image, status, published_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, slug""",
            (title, slug, content, excerpt, meta_description, data.get('keywords', ''), data.get('author', 'Kanchan Ghosh'),
             data.get('featured_image', ''), status, published_at))
        result = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'id': result['id'], 'slug': result['slug']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/blog/posts/<int:post_id>', methods=['PUT'])
def update_blog_post(post_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        conn = get_db()
        cur = conn.cursor()
        updates = []
        values = []
        if 'title' in data:
            updates.append("title = %s")
            values.append(data['title'])
            updates.append("slug = %s")
            values.append(generate_slug(data['title']))
        if 'content' in data:
            updates.append("content = %s")
            values.append(data['content'])
        if 'excerpt' in data:
            updates.append("excerpt = %s")
            values.append(data['excerpt'])
        if 'meta_description' in data:
            updates.append("meta_description = %s")
            values.append(data['meta_description'])
        if 'keywords' in data:
            updates.append("keywords = %s")
            values.append(data['keywords'])
        if 'featured_image' in data:
            updates.append("featured_image = %s")
            values.append(data['featured_image'])
        if 'status' in data:
            updates.append("status = %s")
            values.append(data['status'])
            if data['status'] == 'published':
                cur.execute("SELECT published_at FROM blog_posts WHERE id = %s", (post_id,))
                post = cur.fetchone()
                if not post['published_at']:
                    updates.append("published_at = %s")
                    values.append(datetime.now())
        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(post_id)
        query = f"UPDATE blog_posts SET {', '.join(updates)} WHERE id = %s"
        cur.execute(query, values)
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/blog/posts/<int:post_id>', methods=['DELETE'])
def delete_blog_post(post_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM blog_posts WHERE id = %s", (post_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== PUBLIC BLOG ROUTES ====================
@app.route('/blog')
@app.route('/blog/')
def blog_index():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, slug, excerpt, author, published_at, featured_image
            FROM blog_posts
            WHERE status = 'published'
            ORDER BY published_at DESC
            LIMIT 50
        """)
        posts = cur.fetchall()
        conn.close()

        posts_html = ''
        for post in posts:
            img = post['featured_image'] or 'https://via.placeholder.com/400x250/667eea/ffffff?text=loveUAD'
            date = post['published_at'].strftime('%B %d, %Y') if post.get('published_at') else ''
            excerpt = post.get('excerpt') or ''
            slug = post.get('slug') or ''
            title = post.get('title') or ''
            author = post.get('author') or ''

            posts_html += f'''
            <article class="post-card">
                <a href="/blog/{slug}" class="post-image" style="background-image:url('{img}')"></a>
                <div class="post-content">
                    <div class="post-meta">{date} · {author}</div>
                    <h2><a href="/blog/{slug}">{title}</a></h2>
                    <p>{excerpt}</p>
                    <a href="/blog/{slug}" class="read-more">Read More →</a>
                </div>
            </article>
            '''

        if not posts_html:
            posts_html = '<div class="empty-state"><h2>No posts yet</h2><p>Check back soon for updates!</p></div>'

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Blog | loveUAD - Dementia Care & Family Support</title>
    <meta name="description" content="Latest insights on dementia care, family caregiving, and health technology from loveUAD">
    <style>
        * {{margin:0;padding:0;box-sizing:border-box}}
        body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;color:#333;background:#f9fafb}}
        .header {{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:3rem 2rem;text-align:center}}
        .header h1 {{font-size:3rem;margin-bottom:0.5rem}}
        .header p {{font-size:1.2rem;opacity:0.9}}
        .container {{max-width:1200px;margin:0 auto;padding:3rem 2rem}}
        .posts-grid {{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:2rem}}
        .post-card {{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);transition:transform 0.3s,box-shadow 0.3s}}
        .post-card:hover {{transform:translateY(-4px);box-shadow:0 12px 24px rgba(0,0,0,0.15)}}
        .post-image {{display:block;width:100%;height:250px;background-size:cover;background-position:center}}
        .post-content {{padding:1.5rem}}
        .post-meta {{color:#888;font-size:0.85rem;margin-bottom:0.5rem}}
        .post-content h2 {{margin:0.5rem 0;font-size:1.5rem}}
        .post-content h2 a {{color:#333;text-decoration:none}}
        .post-content h2 a:hover {{color:#667eea}}
        .post-content p {{color:#666;margin:1rem 0}}
        .read-more {{color:#667eea;font-weight:600;text-decoration:none}}
        .read-more:hover {{text-decoration:underline}}
        .empty-state {{text-align:center;padding:4rem 2rem;color:#888}}
        .footer {{background:#1a1a1a;color:#fff;padding:2rem;text-align:center;margin-top:4rem}}
        .footer a {{color:#667eea;text-decoration:none}}
        @media(max-width:768px){{.header h1{{font-size:2rem}}.posts-grid{{grid-template-columns:1fr}}}}
    </style>
</head>
<body>
    <header class="header">
        <h1>loveUAD Blog</h1>
        <p>Insights on dementia care, family support, and health technology</p>
    </header>

    <div class="container">
        <div class="posts-grid">
            {posts_html}
        </div>
    </div>

    <footer class="footer">
        <p>&copy; 2024 loveUAD. <a href="https://loveuad.com">Back to main site</a></p>
    </footer>

    <!-- Metricool -->
    <script>
    (function () {{
        var script = document.createElement("script");
        script.src = "https://tracker.metricool.com/resources/be.js";
        script.async = true;
        script.onload = function () {{
            if (typeof beTracker !== "undefined") {{
                beTracker.t({{ hash: "ef47f15c1ad66c1bd19e05794dd1c95f" }});
            }}
        }};
        document.head.appendChild(script);
    }})();
    </script>
</body>
</html>'''
        return html

    except Exception as e:
        return f"Error: {e}", 500


@app.route('/blog/<slug>')
def blog_post(slug):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM blog_posts WHERE slug = %s AND status = 'published'", (slug,))
        post = cur.fetchone()
        conn.close()

        if not post:
            return "Post not found", 404

        img = post.get('featured_image') or 'https://via.placeholder.com/1200x500/667eea/ffffff?text=loveUAD'
        date = post['published_at'].strftime('%B %d, %Y') if post.get('published_at') else ''
        meta_desc = post.get('meta_description') or post.get('excerpt') or post.get('title') or ''
        keywords = post.get('keywords') or 'dementia care, caregiving, health technology'
        author = post.get('author') or ''
        title = post.get('title') or ''
        content = post.get('content') or ''
        post_id = post.get('id') or 0

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} | loveUAD Blog</title>
    <meta name="description" content="{meta_desc}">
    <meta name="keywords" content="{keywords}">
    <meta name="author" content="{author}">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{meta_desc}">
    <meta property="og:image" content="{img}">
    <meta property="og:type" content="article">
    <style>
        * {{margin:0;padding:0;box-sizing:border-box}}
        body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.8;color:#333;background:#fff}}
        .header {{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:2rem;text-align:center}}
        .header a {{color:#fff;text-decoration:none;font-weight:600}}
        .header a:hover {{opacity:0.8}}

        .hero-frame {{
            width: 60%;
            max-width: 800px;
            height: 300px;
            margin: 20px auto;
            overflow: hidden;
            display: flex;
            justify-content: center;
            align-items: center;
            background: #000;
            border: 3px solid #ff5722;
        }}

        .hero-image {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }}

        @media (max-width: 300px) {{
            .hero-frame {{
                width: 90%;
                height: 150px;
            }}
        }}

        .container {{max-width:800px;margin:0 auto;padding:3rem 2rem}}
        .post-header {{margin-bottom:2rem}}
        .post-meta {{color:#888;font-size:0.9rem;margin-bottom:1rem}}
        h1 {{font-size:2.5rem;margin-bottom:1rem;line-height:1.2}}

        .content {{
            font-size: 1.1rem;
            color: #444;
            white-space: pre-wrap;
        }}

        .content h2 {{margin:2rem 0 1rem;font-size:1.8rem;color:#333}}
        .content h3 {{margin:1.5rem 0 0.75rem;font-size:1.4rem;color:#333}}
        .content p {{margin:1rem 0}}
        .content ul,.content ol {{margin:1rem 0 1rem 2rem}}
        .content li {{margin:0.5rem 0}}
        .content a {{color:#667eea;text-decoration:none;border-bottom:1px solid #667eea}}
        .content a:hover {{opacity:0.8}}
        .content img {{max-width:100%;height:auto;border-radius:8px;margin:1.5rem 0}}
        .content blockquote {{border-left:4px solid #667eea;padding-left:1.5rem;margin:1.5rem 0;font-style:italic;color:#666}}
        .content code {{background:#f4f4f4;padding:2px 6px;border-radius:4px;font-family:monospace}}
        .content pre {{background:#f4f4f4;padding:1rem;border-radius:8px;overflow-x:auto;margin:1.5rem 0}}

        .back-link {{display:inline-block;margin-top:3rem;color:#667eea;text-decoration:none;font-weight:600}}
        .back-link:hover {{text-decoration:underline}}

        .comments-section {{margin-top:3rem;border-top:1px solid #eee;padding-top:2rem}}
        .comment-form input, .comment-form textarea {{
            width:100%; padding:10px; margin-bottom:10px;
            border:1px solid #ddd; border-radius:6px; font-family:inherit;
        }}
        .comment-form button {{
            background:#667eea; color:#fff; padding:10px 20px; border:none;
            border-radius:6px; cursor:pointer; font-weight:600;
        }}
        .comment-form button:hover {{opacity:0.9}}

        .footer {{background:#1a1a1a;color:#fff;padding:2rem;text-align:center;margin-top:4rem}}
        .footer a {{color:#667eea;text-decoration:none}}
        @media(max-width:768px){{h1{{font-size:1.8rem}}}}
    </style>
</head>
<body>
    <div class="header">
        <a href="/blog">← Back to Blog</a>
    </div>

    <div class="hero-frame">
        <img src="{img}" alt="{title}" class="hero-image">
    </div>

    <article class="container">
        <header class="post-header">
            <div class="post-meta">{date} · {author}</div>
            <h1>{title}</h1>
        </header>

        <div class="content">
            {content}
        </div>

        <!-- Comments (ONLY on single post page) -->
        <div class="comments-section">
            <h3>Comments</h3>
            <div id="comments-list">Loading comments...</div>

            <div class="comment-form" style="margin-top:2rem;">
                <h4>Leave a Comment</h4>
                <input type="text" id="comment-name" placeholder="Your Name">
                <textarea id="comment-content" placeholder="Your Comment" style="height:110px;"></textarea>
                <button type="button" onclick="submitComment({post_id})">Post Comment</button>
            </div>
        </div>

        <a href="/blog" class="back-link">← Back to all posts</a>
    </article>

    <footer class="footer">
        <p>&copy; 2024 loveUAD. <a href="https://loveuad.com">Visit main site</a></p>
    </footer>

    <script>
    async function loadComments(postId) {{
        try {{
            const r = await fetch(`/api/blog/posts/${{postId}}/comments`);
            const d = await r.json();
            const list = document.getElementById('comments-list');

            list.innerHTML = (d.comments && d.comments.length > 0)
                ? d.comments.map(c => `
                    <div style="margin-bottom:15px; background:#f9f9f9; padding:12px; border-radius:8px;">
                        <strong>${{c.author_name}}</strong>
                        <small style="color:#888;"> · ${{new Date(c.created_at).toLocaleDateString()}}</small>
                        <p style="margin-top:8px;">${{c.content}}</p>
                    </div>
                `).join('')
                : '<p>No comments yet. Be the first to comment!</p>';
        }} catch (e) {{
            document.getElementById('comments-list').innerHTML = '<p>Could not load comments.</p>';
        }}
    }}

    async function submitComment(postId) {{
        const name = (document.getElementById('comment-name').value || '').trim();
        const content = (document.getElementById('comment-content').value || '').trim();
        if (!name || !content) return;

        const r = await fetch(`/api/blog/posts/${{postId}}/comments`, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name, content}})
        }});

        const out = await r.json();
        if (out && out.success) {{
            document.getElementById('comment-content').value = '';
            loadComments(postId);
        }}
    }}

    loadComments({post_id});
    </script>

    <!-- Metricool -->
    <script>
    (function () {{
        var script = document.createElement("script");
        script.src = "https://tracker.metricool.com/resources/be.js";
        script.async = true;
        script.onload = function () {{
            if (typeof beTracker !== "undefined") {{
                beTracker.t({{ hash: "ef47f15c1ad66c1bd19e05794dd1c95f" }});
            }}
        }};
        document.head.appendChild(script);
    }})();
    </script>
</body>
</html>'''
        return html

    except Exception as e:
        return f"Error: {e}", 500


@app.route('/blog/sitemap.xml')
def blog_sitemap():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT slug, updated_at, published_at FROM blog_posts WHERE status = 'published' ORDER BY published_at DESC")
        posts = cur.fetchall()
        conn.close()
        sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        sitemap += '  <url>\n    <loc>https://blog.loveuad.com/blog</loc>\n    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n  </url>\n'
        for post in posts:
            last_mod = post['updated_at'] or post['published_at']
            sitemap += f'  <url>\n    <loc>https://blog.loveuad.com/blog/{post["slug"]}</loc>\n'
            sitemap += f'    <lastmod>{last_mod.strftime("%Y-%m-%d")}</lastmod>\n'
            sitemap += '    <changefreq>monthly</changefreq>\n    <priority>0.8</priority>\n  </url>\n'
        sitemap += '</urlset>'
        return sitemap, 200, {'Content-Type': 'application/xml'}
    except Exception as e:
        return f"Error: {e}", 500

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
        deletion_requests = fetch_deletion_requests()
        
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
            'ai_compliance': ai_compliance,
            'deletions': deletion_requests
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

@app.route('/api/deletions/process', methods=['POST'])
def process_deletion():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        patient_code = data.get('patient_code')
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("SELECT code_hash FROM deletion_requests WHERE patient_code = %s AND status = 'pending'", (patient_code,))
        result = cur.fetchone()
        
        if not result:
            conn.close()
            return jsonify({'error': 'Request not found'}), 404
        
        code_hash = result['code_hash']
        
        cur.execute("DELETE FROM medications WHERE code_hash = %s", (code_hash,))
        cur.execute("DELETE FROM reminders WHERE code_hash = %s", (code_hash,))
        cur.execute("DELETE FROM patients WHERE code_hash = %s", (code_hash,))
        cur.execute("UPDATE deletion_requests SET status = 'completed', processed_at = CURRENT_TIMESTAMP WHERE patient_code = %s", (patient_code,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/compliance/ai-audit', methods=['GET'])
def get_ai_audit():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        
        code_hash = request.args.get('code_hash')
        limit = int(request.args.get('limit', 100))
        
        if code_hash:
            cur.execute("""
                SELECT action_type, ai_output, user_action, context, model_version, timestamp
                FROM ai_audit_log
                WHERE code_hash = %s
                ORDER BY timestamp DESC
                LIMIT %s
            """, (code_hash, limit))
        else:
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
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#fff}.header{background:#111;padding:20px;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center}.header h1{font-size:1.8rem;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.logout-btn{padding:10px 20px;background:#f87171;color:#fff;border:none;border-radius:6px;cursor:pointer;text-decoration:none}.tabs{display:flex;background:#111;border-bottom:1px solid #333;flex-wrap:wrap}.tab{padding:15px 25px;cursor:pointer;border-bottom:3px solid transparent;position:relative}.tab.active{border-bottom-color:#667eea;background:#1a1a1a}.content{padding:30px;max-width:1600px;margin:0 auto}.tab-content{display:none}.tab-content.active{display:block}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;margin-bottom:30px}.card{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:20px}.card h3{color:#888;font-size:0.85rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}.card .value{font-size:2.5rem;font-weight:bold;margin-bottom:5px}.card .subvalue{color:#888;font-size:0.9rem}.positive{color:#4ade80}.negative{color:#f87171}.neutral{color:#fbbf24}.section{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:25px;margin-bottom:30px}.section h2{margin-bottom:20px;font-size:1.5rem}table{width:100%;border-collapse:collapse}table th,table td{padding:12px;text-align:left;border-bottom:1px solid #333}table th{color:#888;font-weight:600;text-transform:uppercase;font-size:0.85rem}.form-group{margin-bottom:20px}.form-group label{display:block;color:#888;margin-bottom:8px;font-weight:600}.form-group input,.form-group select,.form-group textarea{width:100%;padding:12px;background:#0a0a0a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px}.btn{padding:12px 24px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:14px}.btn:hover{opacity:0.9}.loading{text-align:center;padding:40px;color:#888}.chart-container{position:relative;height:300px;margin-top:20px}.source-badge{display:inline-block;padding:4px 8px;background:#667eea;color:#fff;border-radius:4px;font-size:0.75rem;margin-left:10px}.health-ok{color:#4ade80}.health-bad{color:#f87171}.badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}.badge-error{background:#f87171;color:#fff}.badge-warning{background:#fbbf24;color:#000}.del-badge{background:#f87171;color:#fff;padding:4px 10px;border-radius:6px;font-size:0.85rem;font-weight:600;margin-left:8px}</style></head>
<body><div class="header"><h1>📊 loveUAD Admin Dashboard</h1><div><span id="health-indicator">⏳</span> <a href="/logout" class="logout-btn">Logout</a></div></div>
<div class="tabs"><div class="tab active" onclick="switchTab(event,'financial')">💰 Financial</div><div class="tab" onclick="switchTab(event,'customers')">👥 Customers</div><div class="tab" onclick="switchTab(event,'blog')">📝 Blog</div><div class="tab" onclick="switchTab(event,'errors')">🚨 Errors</div><div class="tab" onclick="switchTab(event,'health')">💓 Health</div><div class="tab" onclick="switchTab(event,'deletions')">🗑️ Deletions <span id="del-badge" class="del-badge" style="display:none">0</span></div></div>
<div class="content"><div class="loading" id="loading">Loading data...</div>

<!-- FINANCIAL TAB -->
<div id="financial-tab" class="tab-content active">
<div class="grid">
<div class="card"><h3>Total Monthly Cost</h3><div class="value negative">$<span id="fin-total-cost">-</span></div><div class="subvalue">Automated + Manual</div></div>
<div class="card"><h3>Monthly Revenue</h3><div class="value positive">$<span id="fin-revenue">-</span></div><div class="subvalue">$5 × <span id="fin-users">-</span> users</div></div>
<div class="card"><h3>Profit / Loss</h3><div class="value" id="fin-profit">$-</div><div class="subvalue">Revenue - Costs</div></div>
<div class="card"><h3>Per User Cost</h3><div class="value neutral">$<span id="fin-per-user">-</span></div><div class="subvalue">Total ÷ users</div></div>
<div class="card"><h3>Breakeven Point</h3><div class="value neutral"><span id="fin-breakeven">-</span></div><div class="subvalue">Users needed</div></div>
</div>
<div class="section"><h2>💸 Cost Breakdown</h2>
<table>
<tr><th>Category</th><th>Amount</th></tr>
<tr><td>☁️ Cloud Run</td><td>$<span id="cost-cloudrun">-</span></td></tr>
<tr><td>🗄️ Cloud SQL</td><td>$<span id="cost-cloudsql">-</span></td></tr>
<tr><td>🌐 Networking</td><td>$<span id="cost-networking">-</span></td></tr>
<tr><td>📞 Twilio</td><td>$<span id="cost-twilio">-</span></td></tr>
<tr><td>🤖 Gemini</td><td>$<span id="cost-gemini">-</span></td></tr>
<tr style="border-top:2px solid #667eea"><td><strong>Automated Total</strong></td><td><strong>$<span id="cost-auto-total">-</span></strong></td></tr>
</table>
</div>
</div>

<!-- CUSTOMERS TAB -->
<div id="customers-tab" class="tab-content">
<div class="grid">
<div class="card"><h3>Total Users</h3><div class="value positive"><span id="users-total">-</span></div></div>
<div class="card"><h3>Active (7d)</h3><div class="value neutral"><span id="users-active">-</span></div><div class="subvalue"><span id="users-active-pct">-</span>% of total</div></div>
<div class="card"><h3>Growth Rate</h3><div class="value" id="users-growth">-</div><div class="subvalue">Week over week</div></div>
<div class="card"><h3>Retention</h3><div class="value neutral"><span id="users-retention">-</span>%</div></div>
</div>
</div>

<!-- BLOG TAB -->
<div id="blog-tab" class="tab-content">
<div class="section">
<div style="display:flex;justify-content:space-between;margin-bottom:20px">
<h2>📝 Blog Posts</h2>
<button class="btn" onclick="showCreatePost()">✨ Create Post</button>
</div>
<table>
<thead><tr><th>Title</th><th>Status</th><th>Published</th><th>Actions</th></tr></thead>
<tbody id="blog-list"></tbody>
</table>
</div>
</div>

<!-- BLOG MODAL -->
<div id="blog-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:1000;align-items:center;justify-content:center">
<div style="background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:30px;max-width:800px;width:90%;max-height:90vh;overflow-y:auto">
<h2 id="modal-title">Create Post</h2>
<form id="blog-form" onsubmit="return false">
<input type="hidden" id="post-id">
<div class="form-group"><label>Title</label><input type="text" id="post-title" required></div>
<div class="form-group"><label>Content (HTML)</label><textarea id="post-content" style="min-height:300px" required></textarea></div>
<div class="form-group"><label>Excerpt</label><textarea id="post-excerpt" rows="3"></textarea></div>
<div class="form-group"><label>Meta Description</label><input type="text" id="post-meta" maxlength="160"></div>
<div class="form-group"><label>Keywords</label><input type="text" id="post-keywords" placeholder="ai, healthcare, dementia"></div>
<div class="form-group"><label>Featured Image URL</label><input type="url" id="post-image"></div>
<div class="form-group"><label>Status</label><select id="post-status"><option value="draft">Draft</option><option value="published">Published</option></select></div>
<div style="display:flex;gap:10px;margin-top:20px">
<button type="button" class="btn" onclick="saveBlogPost()">💾 Save</button>
<button type="button" class="btn" style="background:#f87171" onclick="closeModal()">Cancel</button>
</div>
</form>
</div>
</div>

<!-- ERRORS TAB -->
<div id="errors-tab" class="tab-content">
<div class="grid">
<div class="card"><h3>Errors (24h)</h3><div class="value negative"><span id="errors-24h">-</span></div></div>
<div class="card"><h3>Errors (7d)</h3><div class="value negative"><span id="errors-7d">-</span></div></div>
<div class="card"><h3>Unresolved</h3><div class="value negative"><span id="errors-unresolved">-</span></div></div>
</div>
<div class="section"><h2>Recent Errors</h2>
<table><thead><tr><th>Type</th><th>Message</th><th>Time</th></tr></thead>
<tbody id="error-list"></tbody></table>
</div>
</div>

<!-- HEALTH TAB -->
<div id="health-tab" class="tab-content">
<div class="grid">
<div class="card"><h3>Overall</h3><div class="value" id="health-overall">-</div></div>
<div class="card"><h3>Database</h3><div class="value" id="health-db">-</div><div class="subvalue"><span id="health-db-ms">-</span> ms</div></div>
<div class="card"><h3>Main App</h3><div class="value" id="health-app">-</div><div class="subvalue"><span id="health-app-ms">-</span> ms</div></div>
</div>
</div>

<!-- DELETIONS TAB -->
<div id="deletions-tab" class="tab-content">
<div class="section">
<h2>🗑️ Account Deletion Requests</h2>
<table>
<thead><tr><th>Patient Code</th><th>Requested</th><th>Days Ago</th><th>Action</th></tr></thead>
<tbody id="deletion-list"></tbody>
</table>
</div>
</div>

</div>

<script>
function switchTab(e,tabName){
document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
e.target.classList.add('active');
document.getElementById(tabName+'-tab').classList.add('active');
if(tabName==='blog')loadBlogPosts();
}

async function loadBlogPosts(){
try{
const r=await fetch('/api/blog/posts');
const d=await r.json();
if(!d.success)throw new Error(d.error);
const list=document.getElementById('blog-list');
list.innerHTML=d.posts.length>0
?d.posts.map(p=>`
<tr>
<td>${p.title}</td>
<td><span style="padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;background:${p.status==='published'?'#4ade80':'#888'};color:#fff">${p.status.toUpperCase()}</span></td>
<td>${p.published_at?new Date(p.published_at).toLocaleDateString():'—'}</td>
<td>
<button class="btn" style="padding:8px 16px;font-size:12px;margin-right:5px" onclick="editPost(${p.id})">Edit</button>
<button class="btn" style="padding:8px 16px;font-size:12px;margin-right:5px" onclick="viewPost('${p.slug}')">View</button>
<button class="btn" style="padding:8px 16px;font-size:12px;background:#f87171" onclick="deletePost(${p.id})">Delete</button>
</td>
</tr>
`).join('')
:'<tr><td colspan="4" style="text-align:center;color:#888">No posts yet. Create your first post!</td></tr>';
}catch(e){
alert('Failed to load posts: '+e.message);
}
}

function showCreatePost(){
document.getElementById('modal-title').textContent='Create Post';
document.getElementById('blog-form').reset();
document.getElementById('post-id').value='';
document.getElementById('blog-modal').style.display='flex';
}

function closeModal(){
document.getElementById('blog-modal').style.display='none';
}

async function editPost(id){
try{
const r=await fetch(`/api/blog/posts/${id}`);
const d=await r.json();
if(!d.success)throw new Error(d.error);
const p=d.post;
document.getElementById('modal-title').textContent='Edit Post';
document.getElementById('post-id').value=p.id;
document.getElementById('post-title').value=p.title;
document.getElementById('post-content').value=p.content;
document.getElementById('post-excerpt').value=p.excerpt||'';
document.getElementById('post-meta').value=p.meta_description||'';
document.getElementById('post-keywords').value=p.keywords||'';
document.getElementById('post-image').value=p.featured_image||'';
document.getElementById('post-status').value=p.status;
document.getElementById('blog-modal').style.display='flex';
}catch(e){
alert('Failed to load post: '+e.message);
}
}

async function saveBlogPost(){
try{
const id=document.getElementById('post-id').value;
const data={
title:document.getElementById('post-title').value,
content:document.getElementById('post-content').value,
excerpt:document.getElementById('post-excerpt').value,
meta_description:document.getElementById('post-meta').value,
keywords:document.getElementById('post-keywords').value,
featured_image:document.getElementById('post-image').value,
status:document.getElementById('post-status').value
};
const url=id?`/api/blog/posts/${id}`:'/api/blog/posts';
const method=id?'PUT':'POST';
const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const result=await r.json();
if(!result.success)throw new Error(result.error);
alert('✓ Post saved!');
closeModal();
loadBlogPosts();
}catch(e){
alert('Failed to save: '+e.message);
}
}

async function deletePost(id){
if(!confirm('Delete this post?'))return;
try{
const r=await fetch(`/api/blog/posts/${id}`,{method:'DELETE'});
const d=await r.json();
if(!d.success)throw new Error(d.error);
alert('✓ Post deleted');
loadBlogPosts();
}catch(e){
alert('Failed to delete: '+e.message);
}
}

function viewPost(slug){
window.open(`/blog/${slug}`,'_blank');
}

async function loadMetrics(){
try{
const r=await fetch('/api/metrics');
const d=await r.json();
if(!d.success)throw new Error(d.error);

document.getElementById('loading').style.display='none';

// Financial
document.getElementById('fin-total-cost').textContent=d.financial.total_costs.toFixed(2);
document.getElementById('fin-revenue').textContent=d.financial.monthly_revenue.toFixed(2);
document.getElementById('fin-users').textContent=d.financial.current_users;
document.getElementById('fin-per-user').textContent=d.costs.per_user.toFixed(2);
document.getElementById('fin-breakeven').textContent=d.financial.breakeven_users;
const profit=d.financial.profit_loss;
const profitEl=document.getElementById('fin-profit');
profitEl.textContent='$'+profit.toFixed(2);
profitEl.className=profit>=0?'value positive':'value negative';

// Costs
document.getElementById('cost-cloudrun').textContent=d.costs.automated.cloud_run.toFixed(2);
document.getElementById('cost-cloudsql').textContent=d.costs.automated.cloud_sql.toFixed(2);
document.getElementById('cost-networking').textContent=d.costs.automated.networking.toFixed(2);
document.getElementById('cost-twilio').textContent=d.costs.automated.twilio.toFixed(2);
document.getElementById('cost-gemini').textContent=d.costs.automated.gemini.toFixed(2);
document.getElementById('cost-auto-total').textContent=d.costs.automated.total.toFixed(2);

// Users
document.getElementById('users-total').textContent=d.users.total_users;
document.getElementById('users-active').textContent=d.users.active_once_7d;
document.getElementById('users-active-pct').textContent=d.users.active_once_pct;
const growth=d.users.growth_rate;
const growthEl=document.getElementById('users-growth');
growthEl.textContent=growth.toFixed(1)+'%';
growthEl.className=growth>=0?'value positive':'value negative';
document.getElementById('users-retention').textContent=d.users.retention_rate.toFixed(1);

// Errors
document.getElementById('errors-24h').textContent=d.errors.errors_24h;
document.getElementById('errors-7d').textContent=d.errors.errors_7d;
document.getElementById('errors-unresolved').textContent=d.errors.unresolved;
const errorList=document.getElementById('error-list');
errorList.innerHTML=d.errors.recent.slice(0,20).map(e=>`<tr><td><span class="badge badge-error">${e.error_type}</span></td><td>${e.message.substring(0,80)}</td><td>${new Date(e.created_at).toLocaleString()}</td></tr>`).join('');

// Health
const health=d.health;
document.getElementById('health-overall').textContent=health.overall;
document.getElementById('health-overall').className=health.overall==='healthy'?'value health-ok':'value health-bad';
document.getElementById('health-db').textContent=health.database.status;
document.getElementById('health-db').className=health.database.status==='ok'?'value health-ok':'value health-bad';
document.getElementById('health-db-ms').textContent=health.database.response_ms;
document.getElementById('health-app').textContent=health.main_app.status;
document.getElementById('health-app').className=health.main_app.status==='ok'?'value health-ok':'value health-bad';
document.getElementById('health-app-ms').textContent=health.main_app.response_ms;
document.getElementById('health-indicator').textContent=health.overall==='healthy'?'✅':'❌';

// Deletions
updateDeletionsTab(d);

}catch(e){
document.getElementById('loading').textContent='Error: '+e.message;
}
}

function updateDeletionsTab(d){
const dels=d.deletions||[];
const list=document.getElementById('deletion-list');
const badge=document.getElementById('del-badge');

if(dels.length>0){
badge.textContent=dels.length;
badge.style.display='inline-block';
}else{
badge.style.display='none';
}

list.innerHTML=dels.length>0
?dels.map(r=>`
<tr id="del-${r.patient_code}">
<td>${r.patient_code}</td>
<td>${new Date(r.requested_at).toLocaleString()}</td>
<td>${Math.floor(r.days_pending||0)}</td>
<td><button class="btn" onclick="deletePermanently('${r.patient_code}')">🗑️ Delete</button></td>
</tr>
`).join('')
:'<tr><td colspan="4">No pending requests</td></tr>';
}

async function deletePermanently(patientCode){
if(!confirm(`PERMANENTLY DELETE account ${patientCode}?\n\nThis will remove:\n• All patient data\n• All medications\n• All reminders\n\nThis action CANNOT be undone!`)){
return;
}

try{
const response=await fetch('/api/deletions/process',{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({patient_code:patientCode})
});

const data=await response.json();

if(data.success){
const row=document.getElementById(`del-${patientCode}`);
if(row)row.remove();
alert('✓ Account deleted successfully');
loadMetrics();
}else{
alert('Error: '+data.error);
}
}catch(error){
alert('Delete failed: '+error.message);
}
}

loadMetrics();
setInterval(loadMetrics,30000);
</script>
</body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
