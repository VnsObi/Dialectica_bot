with open('requirements.txt', 'r') as f:
    lines = [l.strip() for l in f.readlines() if l.strip()]

out = []
for l in lines:
    if l not in out and l != 'tenacity':
        out.append(l)

with open('requirements.txt', 'w') as f:
    f.write('\n'.join(out) + '\n')
