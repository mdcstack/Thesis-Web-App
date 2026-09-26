import re

with open('templates/receipts.html', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('<div class="card-actions">', '<div class="card-actions">\n                    <a href="/distribution" id="backBtn" class="btn-action-top" style="background-color: #f39c12; color: white; text-decoration: none; display: none;">⬅ Back to Distribution</a>')

script_addition = """
    // Check if an Admin is logged in, if so, reveal the Back button
    const activeUser = JSON.parse(localStorage.getItem('bookletlog_user'));
    if (activeUser && activeUser.session_token) {
        document.getElementById('backBtn').style.display = 'flex';
    }
</script>
</body>
"""

content = content.replace('</script>\n</body>', script_addition)

with open('templates/receipts.html', 'w', encoding='utf-8') as f:
    f.write(content)
