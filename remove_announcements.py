"""Remove Discord announcement blocks from jobs.py"""
with open('scheduler/jobs.py', encoding='utf-8') as f:
    lines = f.readlines()

# Find and remove the two announcement blocks
result = []
i = 0
skip_patterns = [
    'send_upload_announcement(',
    '# \u2500\u2500 Step 6: Discord announcement',
    '# \u2500\u2500 6. Discord Announcement (Optional)',
]

while i < len(lines):
    line = lines[i]
    
    # Check if this line starts an announcement block to skip
    is_block_start = any(pat in line for pat in skip_patterns)
    
    if is_block_start:
        # Skip until we hit the next non-indented comment or empty line after block
        # The blocks end after the "if msg_id:" section
        # Count indentation of block start
        indent = len(line) - len(line.lstrip())
        
        # Skip lines that are part of this block
        # Block ends when we see a line with less or equal indent that is not a continuation
        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            if next_line.strip() == '':
                # Empty line might end the block - check next line
                if j + 1 < len(lines):
                    after = lines[j + 1]
                    after_indent = len(after) - len(after.lstrip()) if after.strip() else 999
                    if after.strip() and after_indent <= indent:
                        break  # block ended
                j += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_line.strip() and next_indent <= indent:
                break
            j += 1
        
        i = j  # skip the entire block
        # Remove the trailing empty line we stopped at
        if i < len(lines) and lines[i].strip() == '':
            i += 1
    else:
        result.append(line)
        i += 1

with open('scheduler/jobs.py', 'w', encoding='utf-8') as f:
    f.writelines(result)

print(f"Done. Lines: {len(lines)} -> {len(result)}")
# Verify no more send_upload_announcement
remaining = [l for l in result if 'send_upload_announcement' in l]
print(f"Remaining references: {len(remaining)}")
for l in remaining:
    print(' ', repr(l))
