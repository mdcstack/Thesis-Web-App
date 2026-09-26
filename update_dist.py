import re

with open('templates/distribution.html', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('target="_blank" class="btn" style="width: auto; margin: 0; background-color: #8e44ad; text-decoration: none;">', 'class="btn" style="width: auto; margin: 0; background-color: #8e44ad; text-decoration: none;">')

with open('templates/distribution.html', 'w', encoding='utf-8') as f:
    f.write(content)
