import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importers import parse_docx

source = Path(sys.argv[1])
questions, errors, summary = [], [], {}
for file in sorted(source.glob('*.docx'), key=lambda p:int(p.stem.split('.')[0])):
    category = file.stem.split('.', 1)[1]
    parsed, failed = parse_docx(file, category)
    questions.extend(parsed)
    errors.extend(failed)
    summary[category] = len(parsed)
target = Path(__file__).resolve().parents[1] / 'seed'
(target / 'questions.json').write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding='utf-8')
(target / 'import-report.json').write_text(json.dumps(dict(categories=summary, total=len(questions), errors=errors), ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(dict(categories=summary, total=len(questions), errors=errors), ensure_ascii=False))
if errors:
    sys.exit(1)
