"""Refresh saved evidence status; never publish journal entries."""
import argparse
from journal_core import Registry, atomic, digest, guarded, now, read_json


def generate_status(reg):
    lines = ['# 实施与验收状态', '', f'报告生成时间：{now()}。项目：`{reg.base}`。', '',
             '以下列出验收报告的时间和实现哈希。历史通过不代表当前版本已复验。', '']
    reports = [reg.base / 'docs' / name for name in (
        'acceptance-runtime.json', 'native-codex-runtime.json', 'obsidian-runtime.json',
        'backup-restore-runtime.json')]
    reports += sorted((reg.base / 'docs/reviews').glob('*/runtime.json'))
    found = False
    for path in reports:
        if not path.is_file():
            continue
        found = True
        relative = path.relative_to(reg.base).as_posix()
        lines += [f'## {relative}', '']
        try:
            report = read_json(path)
            if not isinstance(report, dict):
                raise ValueError('Report must be an object')
            lines += [f"记录时间：{report.get('observed_at', report.get('timestamp', '未提供'))}。"]
            hashes = report.get('installed_hashes', {})
            if not isinstance(hashes, dict):
                raise ValueError('Source hashes must be an object')
            matching = bool(hashes) and all(
                guarded(reg.base, name).is_file() and digest(guarded(reg.base, name).read_bytes()) == value
                for name, value in hashes.items())
            lines += ['报告所列文件哈希：' + ('与当前文件一致。' if matching else '已变化或未提供；作为历史证据。')]
            for key in ('status', 'installed_suite', 'automatic_desktop_hook_e2e',
                        'real_90_second_timeout', 'obsidian_client', 'scale_benchmark'):
                if key in report:
                    value = report[key]
                    if isinstance(value, dict):
                        fields = ('status', 'tests', 'exit_code', 'timed_out', 'seconds',
                                  'elapsed_seconds', 'event_reads_per_query', 'notes')
                        value = ', '.join(f'{field}={value[field]}' for field in fields
                                          if field in value and not isinstance(value[field], (dict, list)))
                    value = str(value or '详见原始报告').replace('`', '').replace('\n', ' ')[:200]
                    lines.append(f'- `{key}`：{value}')
            lines += [f'原始报告：[{relative}]({relative.removeprefix("docs/")})', '']
        except (OSError, ValueError, TypeError) as exc:
            lines += [f'报告无法读取，状态未确认：{type(exc).__name__}。', '']
    if not found:
        lines += ['未运行：尚无本地验收报告。', '']
    lines += ['## 维护入口', '',
              '`python finalize.py` 更新本摘要；验收报告、事件、回执和受管笔记保持原样。',
              '`python journal.py events-index` 重建未处理事件索引，保留原始事件和 ACK。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    args = parser.parse_args()
    reg = Registry(args.config)
    path = reg.base / 'docs/implementation-status.md'
    atomic(path, generate_status(reg))
    print(path)


if __name__ == '__main__':
    main()
