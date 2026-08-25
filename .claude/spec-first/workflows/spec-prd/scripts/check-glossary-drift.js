#!/usr/bin/env node
'use strict';

// 确定性术语 drift 检测:扫描目标 PRD 文本中 canonical glossary 的 avoid 别名字面命中。
// 这是 script-owned facts —— 脚本只报告"哪里字面出现了 avoid 术语",是否构成真实问题
// (引用、讨论该术语本身、还是真的用错词)由 LLM 在 readiness 判断中裁决。
// 脚本不提取新术语、不判断定义是否偏离 canonical —— 那些是 LLM-owned 语义判断。

const fs = require('fs');
const path = require('path');

const DEFAULT_GLOSSARY = 'docs/contracts/domain-glossary.md';

function parseArgs(argv) {
  const args = { target: null, glossary: DEFAULT_GLOSSARY, error: null };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--glossary') {
      if (!argv[i + 1] || argv[i + 1].startsWith('--')) {
        args.error = 'missing value for --glossary';
        break;
      }
      args.glossary = argv[i + 1];
      i += 1;
    } else if (!args.target) {
      args.target = a;
    } else {
      args.error = `unexpected extra argument: ${a}`;
      break;
    }
  }
  return args;
}

// 解析 glossary 的 ### 条目,提取 { canonical_name, avoid[], status }
function parseGlossary(text) {
  // 先剥离 fenced code blocks —— glossary 文档会内嵌格式示例(``` 代码块里的
  // 示例 ### 条目不是真实条目),不剥离会把示例误当成 canonical term。
  const withoutFences = text.replace(/^```[\s\S]*?^```/gm, '');
  const entries = [];
  const blocks = withoutFences.split(/^### /m).slice(1);
  for (const block of blocks) {
    const lines = block.split('\n');
    const canonical = lines[0].trim();
    if (!canonical) continue;
    const avoidLine = lines.find((l) => /^-\s*avoid\s*:/i.test(l));
    const statusLine = lines.find((l) => /^-\s*status\s*:/i.test(l));
    const avoid = avoidLine
      ? avoidLine
          .replace(/^-\s*avoid\s*:/i, '')
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const status = statusLine
      ? statusLine.replace(/^-\s*status\s*:/i, '').trim()
      : 'active';
    entries.push({ canonical_name: canonical, avoid, status });
  }
  return entries;
}

// 字面查找一个 alias 在一行中是否出现。
// 不用正则 —— 正则的 `g` flag 有状态(lastIndex 跨调用累积会漏报),
// 且 `\b` 词边界对以非字母数字结尾的术语(C++、.NET、C#)永远失败。
// 改为无状态的 indexOf 扫描 + 显式边界判断:
//   - 纯 ASCII 字母数字术语要求"整词"(两侧非字母数字),避免 bill 命中 billing;
//   - 含非 ASCII(中文等)或非字母数字字符的术语用纯字面匹配,
//     因为这类术语没有可靠的词边界概念,误报由 LLM 在 readiness 裁决兜底。
function lineContainsAlias(line, alias) {
  if (!alias) return false;
  const hay = line.toLowerCase();
  const needle = alias.toLowerCase();
  const isAsciiWord = /^[a-z0-9]+$/i.test(alias);
  let from = 0;
  for (;;) {
    const idx = hay.indexOf(needle, from);
    if (idx === -1) return false;
    if (!isAsciiWord) return true;
    const before = idx === 0 ? '' : hay[idx - 1];
    const after = idx + needle.length >= hay.length ? '' : hay[idx + needle.length];
    const boundedLeft = before === '' || !/[a-z0-9]/i.test(before);
    const boundedRight = after === '' || !/[a-z0-9]/i.test(after);
    if (boundedLeft && boundedRight) return true;
    from = idx + needle.length;
  }
}

function detectDrift(targetText, entries) {
  const findings = [];
  const lines = targetText.split('\n');
  for (const entry of entries) {
    for (const alias of entry.avoid) {
      lines.forEach((line, idx) => {
        if (lineContainsAlias(line, alias)) {
          findings.push({
            reason_code: 'avoid_term_used',
            term_used: alias,
            canonical_name: entry.canonical_name,
            canonical_status: entry.status,
            line: idx + 1,
          });
        }
      });
    }
  }
  return findings;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.error || !args.target) {
    if (args.error) {
      process.stderr.write(`${args.error}\n`);
    }
    process.stderr.write(
      'usage: check-glossary-drift.js <target-prd-path> [--glossary <path>]\n'
    );
    process.exit(2);
  }

  let targetText;
  try {
    targetText = fs.readFileSync(path.resolve(args.target), 'utf8');
  } catch (err) {
    process.stderr.write(`cannot read target: ${args.target}\n`);
    process.exit(2);
  }

  const result = {
    glossary: args.glossary,
    target: args.target,
    glossary_status: 'present',
    findings: [],
  };

  let glossaryText;
  try {
    glossaryText = fs.readFileSync(path.resolve(args.glossary), 'utf8');
  } catch (err) {
    // glossary 是 opt-in;不存在则优雅降级为无 drift,不报错
    result.glossary_status = 'absent';
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    process.exit(0);
  }

  const entries = parseGlossary(glossaryText);
  if (entries.length === 0) {
    result.glossary_status = 'empty';
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    process.exit(0);
  }

  result.findings = detectDrift(targetText, entries);
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  process.exit(0);
}

main();
