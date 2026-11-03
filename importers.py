import re
import zipfile
from docx import Document
from openpyxl import load_workbook


def validate_zip(source):
    with zipfile.ZipFile(source) as z:
        if len(z.infolist())>2000 or sum(i.file_size for i in z.infolist())>32*1024*1024:
            raise ValueError('文件解压后超过32MB或结构过于复杂，请拆分后导入')
    if hasattr(source,'seek'): source.seek(0)


def parse_docx(source, category):
    validate_zip(source)
    paragraphs = [p.text.strip() for p in Document(source).paragraphs if p.text.strip()]
    # Word automatic list numbering is stored in numbering.xml, absent from p.text.
    question_paragraphs = [p for p in paragraphs if re.search(r'\nA[.．、]',p)]
    if question_paragraphs and all('答案' in p and '解析' in p for p in question_paragraphs):
        text = '\n'.join(f'{i}. '+re.sub(r'^\d+[.．、]\s*','',p) for i,p in enumerate(question_paragraphs,1))
    else:
        text = '\n'.join(paragraphs)
    blocks = re.split(r'(?m)^\s*(\d+)[.．、]\s*', text)
    result, errors = [], []
    for i in range(1, len(blocks), 2):
        number, body = blocks[i:i+2]
        match = re.match(r'(.*?)\nA[.．、]\s*(.*?)\nB[.．、]\s*(.*?)\nC[.．、]\s*(.*?)\nD[.．、]\s*(.*?)\n答案[：:]\s*([A-D])\s*\n解析[：:]\s*(.*)', body.strip(), re.S)
        if not match:
            errors.append(f'{category} 第{number}题：结构无法识别')
            continue
        stem, a, b, c, d, answer, explanation = match.groups()
        result.append(dict(category=category, number=int(number), stem=stem.strip(), options=[x.strip() for x in (a,b,c,d)], answer=answer, explanation=explanation.strip()))
    if not result and not errors:
        errors.append('没有识别到题目，请使用“1.题干 / A.选项 / 答案：A / 解析：…”格式')
    return result, errors


def parse_students(source, default_class=''):
    validate_zip(source)
    workbook = load_workbook(source, read_only=True, data_only=True)
    result = []
    try:
        for sheet in workbook:
            headers = None
            for rowno, row in enumerate(sheet.iter_rows(values_only=True), 1):
                values = [str(v).strip() if v is not None else '' for v in row]
                if headers is None:
                    aliases = {'班级': 'class', '行政班级': 'class', '学号': 'number', '学生学号': 'number', '姓名': 'name', '学生姓名': 'name'}
                    mapping = {aliases[v]: i for i,v in enumerate(values) if v in aliases}
                    if {'number','name'} <= set(mapping) and ('class' in mapping or default_class):
                        headers = mapping
                    continue
                if not any(values):
                    continue
                item = {k: values[v] for k,v in headers.items()}
                item.setdefault('class', default_class)
                if not all(item.values()):
                    raise ValueError(f'{sheet.title} 第{rowno}行缺少班级、学号或姓名')
                if item['number'].endswith('.0'):
                    item['number'] = item['number'][:-2]
                result.append(item)
    finally:
        workbook.close()
    if not result:
        raise ValueError('未找到学生记录，表头必须包含：班级、学号、姓名')
    if len({x['number'] for x in result}) != len(result):
        raise ValueError('文件中学号重复，请修正后重试')
    return result
