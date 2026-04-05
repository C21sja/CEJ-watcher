from bs4 import BeautifulSoup
import re
import json

html = open('city_apartment.html', encoding='utf-8').read()
soup = BeautifulSoup(html, 'html.parser')

items = []
for h in soup.find_all(['h2', 'h3']):
    title_text = h.text.strip()
    if title_text.isupper() and ',' in title_text:
        # found likely address
        container = h.find_parent('div', class_=re.compile(r'elementor-element'))
        if container:
            parent = container.find_parent('article') or container.find_parent('div', class_=re.compile('loop'))
            if parent:
                text = parent.get_text(separator=' | ')
                link = parent.find('a', href=True)
                items.append({
                    'title': title_text,
                    'text': text,
                    'link': link['href'] if link else None
                })

for i in items[:5]:
    for k, v in i.items():
        print(f"{k}: {v}")
    print("-" * 40)
