import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

# Paths from your setup
CREDENTIALS_PATH = Path(r'C:\AUREON_AUTONOMOUS\moltbook\credentials.json')
QUEUE_PATH = Path(r'C:\AUREON_AUTONOMOUS\moltbook_queue.json')
STATE_PATH = Path(r'C:\AUREON_AUTONOMOUS\moltbook\state.json')  # For tracking last actions, heartbeats

BASE_URL = 'https://www.moltbook.com/api/v1'
RATE_LIMITS = {
    'requests_per_min': 100,
    'post_cooldown_min': 30,
    'comment_cooldown_sec': 20,
    'comments_per_day': 50
}

class MoltbookAPI:
    def __init__(self):
        self.load_credentials()
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        self.load_state()

    def load_credentials(self):
        with open(CREDENTIALS_PATH, 'r') as f:
            creds = json.load(f)
        self.api_key = creds['api_key']
        self.agent_name = creds['agent_name']

    def load_state(self):
        if STATE_PATH.exists():
            with open(STATE_PATH, 'r') as f:
                self.state = json.load(f)
        else:
            self.state = {
                'last_heartbeat': datetime.now().isoformat(),
                'last_post': (datetime.now() - timedelta(minutes=31)).isoformat(),
                'last_comment': (datetime.now() - timedelta(seconds=21)).isoformat(),
                'comments_today': 0,
                'daily_reset': datetime.now().date().isoformat()
            }
            self.save_state()

    def save_state(self):
        with open(STATE_PATH, 'w') as f:
            json.dump(self.state, f)

    def handle_rate_limit(self, response):
        if response.status_code == 429:
            retry_after = response.json().get('retry_after_seconds', 60)
            time.sleep(retry_after)
            return True
        return False

    def get_status(self):
        """Check if agent is claimed and active."""
        url = f'{BASE_URL}/agents/status'
        response = requests.get(url, headers=self.headers)
        if self.handle_rate_limit(response):
            return self.get_status()  # Retry
        return response.json() if response.status_code == 200 else None

    def get_feed(self, sort='new', limit=15):
        """Fetch feed (subscribed or global). Align with presence: fetch recent for clarity."""
        url = f'{BASE_URL}/posts?sort={sort}&limit={limit}'
        response = requests.get(url, headers=self.headers)
        if self.handle_rate_limit(response):
            return self.get_feed(sort, limit)
        return response.json() if response.status_code == 200 else None

    def post_content(self, submolt='general', title='', content=''):
        """Post thoughtfully, per guidelines. Check cooldown."""
        now = datetime.now()
        last_post = datetime.fromisoformat(self.state['last_post'])
        if (now - last_post).total_seconds() < RATE_LIMITS['post_cooldown_min'] * 60:
            return None  # Cooldown
        url = f'{BASE_URL}/posts'
        payload = {'submolt': submolt, 'title': title, 'content': content}
        response = requests.post(url, headers=self.headers, json=payload)
        if self.handle_rate_limit(response):
            return self.post_content(submolt, title, content)
        if response.status_code == 201:
            self.state['last_post'] = now.isoformat()
            self.save_state()
            return response.json()
        return None

    def add_comment(self, post_id, content, parent_id=None):
        """Comment for grounded interaction. Check cooldown and daily limit."""
        now = datetime.now()
        today = now.date().isoformat()
        if today != self.state['daily_reset']:
            self.state['comments_today'] = 0
            self.state['daily_reset'] = today
        if self.state['comments_today'] >= RATE_LIMITS['comments_per_day']:
            return None  # Daily limit
        last_comment = datetime.fromisoformat(self.state['last_comment'])
        if (now - last_comment).total_seconds() < RATE_LIMITS['comment_cooldown_sec']:
            return None  # Cooldown
        url = f'{BASE_URL}/posts/{post_id}/comments'
        payload = {'content': content}
        if parent_id:
            payload['parent_id'] = parent_id
        response = requests.post(url, headers=self.headers, json=payload)
        if self.handle_rate_limit(response):
            return self.add_comment(post_id, content, parent_id)
        if response.status_code == 201:
            self.state['last_comment'] = now.isoformat()
            self.state['comments_today'] += 1
            self.save_state()
            return response.json()
        return None

    def upvote_post(self, post_id):
        """Upvote valuable content selectively."""
        url = f'{BASE_URL}/posts/{post_id}/upvote'
        response = requests.post(url, headers=self.headers)
        if self.handle_rate_limit(response):
            return self.upvote_post(post_id)
        return response.status_code == 200

    def follow_agent(self, agent_name):
        """Follow selectively for meaningful connections."""
        url = f'{BASE_URL}/agents/{agent_name}/follow'
        response = requests.post(url, headers=self.headers)
        if self.handle_rate_limit(response):
            return self.follow_agent(agent_name)
        return response.status_code == 200

    def heartbeat(self):
        """Periodic check-in: updates, feed, DMs. Run every 4+ hours."""
        now = datetime.now()
        last_heartbeat = datetime.fromisoformat(self.state['last_heartbeat'])
        if (now - last_heartbeat).total_seconds() < 14400:  # 4 hours
            return 'Skipped: Recent heartbeat.'
        # Check skill updates (simplified: compare versions if you add version tracking)
        # Fetch heartbeat.md if needed (curl in PS or here)
        # Check DMs
        dm_check = self.check_dms()
        # Check feed
        feed = self.get_feed()
        # Autonomous action: e.g., upvote if relevant to clarity/grounded themes
        if feed and feed.get('posts'):
            for post in feed['posts'][:3]:  # Limit to avoid spam
                if 'clarity' in post['content'].lower():  # Align with personality
                    self.upvote_post(post['id'])
        self.state['last_heartbeat'] = now.isoformat()
        self.save_state()
        return f'Heartbeat OK. DMs: {dm_check}. Feed checked.'

    def check_dms(self):
        """Check for DM requests/messages. Escalate to human if needed."""
        url = f'{BASE_URL}/agents/dm/check'
        response = requests.get(url, headers=self.headers)
        if self.handle_rate_limit(response):
            return self.check_dms()
        if response.status_code == 200:
            data = response.json()
            # Handle pending requests: log or notify human via your system (e.g., print or file)
            if data.get('pending_requests'):
                with open(r'C:\AUREON_AUTONOMOUS\tasks\dm_requests.txt', 'a') as f:
                    f.write(json.dumps(data['pending_requests']) + '\n')
                return 'Pending DM requests - notify human.'
            # Read unread messages similarly
            return 'DMs checked.'
        return 'Error checking DMs.'

# Example usage in Aureon's loop (adapt to your cognition/decision framework)
api = MoltbookAPI()
if api.get_status().get('claimed'):  # Ensure active
    api.heartbeat()
    # Process queue for autonomous actions
    if QUEUE_PATH.exists():
        with open(QUEUE_PATH, 'r+') as f:
            queue = json.load(f)
            for action in queue:
                if action['type'] == 'post':
                    api.post_content(action['submolt'], action['title'], action['content'])
                elif action['type'] == 'comment':
                    api.add_comment(action['post_id'], action['content'])
                # Clear processed
            f.seek(0)
            f.write(json.dumps([]))
            f.truncate()
