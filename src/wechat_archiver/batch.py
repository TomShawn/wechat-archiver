import json, re, shutil, uuid
from pathlib import Path
from urllib.parse import quote
from .storage import atomic_write, write_json, digest, link_or_copy
from .urls import article_id
from .models import now

def export_batch(archiver, inputs, destination, summary):
    destination=Path(destination).resolve()
    destination.mkdir(parents=True,exist_ok=True)
    entries=[]
    emitted={}
    for spec in inputs:
        key=article_id(spec['url']) if spec.get('url') else digest(str(Path(spec['path']).resolve()).encode())
        row=archiver.database.get(key)
        entry={'input':spec,'status':row['status'] if row else 'failed','error':row.get('error') if row else '输入无效'}
        if row and row.get('local_path') and Path(row['local_path'],'index.md').exists():
            source=Path(row['local_path'])
            if str(source) in emitted:
                entry.update(title=row['title'],file=emitted[str(source)])
                entries.append(entry)
                continue
            title=re.sub(r'[\x00-\x1f/\\:*?"<>|]','_',row['title']).strip(' .') or '未命名文章'
            while len(title.encode())>220: title=title[:-1]
            name=title+'.md'
            target=destination/name
            if target.exists(): name=title+'--'+key[:10]+'.md'
            resources=destination/'assets'/key
            resources.mkdir(parents=True,exist_ok=True)
            if (source/'assets').exists(): shutil.copytree(source/'assets',resources,dirs_exist_ok=True,copy_function=link_or_copy)
            text=(source/'index.md').read_text()
            text=text.replace('assets/', 'assets/'+key+'/')
            atomic_write(destination/name,text)
            emitted[str(source)]=name
            originals=destination/'originals'/key
            originals.mkdir(parents=True,exist_ok=True)
            for original in ('original.html','original.md','quality.json'):
                if (source/original).exists(): shutil.copy2(source/original,originals/original)
            entry.update(title=row['title'],file=name,quality=json.loads((source/'quality.json').read_text()))
        entries.append(entry)
    report={'created_at':now(),'summary':summary.to_dict(),'articles':entries}
    write_json(destination/'report.json',report)
    lines=['# 批次归档报告','',f'输入 {len(inputs)} 项；详情见 report.json。','']
    for entry in entries:
        label=entry.get('title') or entry['input'].get('url') or entry['input'].get('path')
        if entry.get('file'): lines.append(f"- [{label}]({quote(entry['file'])}) — {entry['status']}")
        else: lines.append(f"- {label} — {entry['status']}：{entry.get('error')}")
    (destination/'reports').mkdir(exist_ok=True)
    atomic_write(destination/'reports'/'index.md','\n'.join(lines).replace('](','](../'))
    return str(destination)
