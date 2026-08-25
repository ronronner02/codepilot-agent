#!/usr/bin/env node
'use strict';

const path = require('node:path');

const {
  classifyEventKind,
  classifyStates,
  evidence,
  hashText,
  makeArtifact,
  parseCommonArgs,
  redactForArtifactText,
  readText,
  resolveBoundedInputPath,
  slugify,
  sourceInputFromFile,
  toPosix,
  unique,
  unavailableSourceInput,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractPrdContract(options = {}) {
  const prdPath = options.prd;
  if (!prdPath) {
    return makeArtifact({
      schemaVersion: 'product-contract.v1',
      artifactId: 'product-contract',
      sourceInputs: [unavailableSourceInput('prd', 'prd', 'prd_missing')],
      body: {
        features: [],
        pages: [],
        degraded_modes: [{
          code: 'prd_missing',
          severity: 'warning',
          summary: 'PRD input was not provided.',
          path: null,
        }],
      },
    });
  }

  const initialPrd = path.resolve(prdPath);
  const repoRoot = path.resolve(options.repoRoot || options.source || path.dirname(initialPrd));
  const resolution = resolveBoundedInputPath({
    repoRoot,
    inputPath: prdPath,
    kind: 'prd',
    expected: 'file',
    maxBytes: options.maxBytes || 512 * 1024,
    allowOutside: options.allowOutside || options.allowOutsidePaths || [],
  });
  if (!resolution.ok) {
    throw new Error(`prd rejected: ${resolution.reason}`);
  }

  const absolutePrd = resolution.realpath;
  const text = readText(absolutePrd, options.maxBytes || 512 * 1024);
  const sections = splitSections(text);
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const pages = extractPages(lines, absolutePrd, repoRoot);
  const journeys = extractJourneys(lines, absolutePrd, repoRoot);
  const businessRules = extractBusinessRules(lines, absolutePrd, repoRoot);
  const analyticsRequirements = extractAnalyticsRequirements(lines, absolutePrd, repoRoot);
  const i18nRequirements = extractI18nRequirements(lines, absolutePrd, repoRoot);
  const industrySignals = extractIndustrySignals(text);
  const states = classifyStates(text);

  const featureName = redactForArtifactText(firstHeading(sections) || path.basename(absolutePrd, path.extname(absolutePrd)), { maxLength: 120 });
  const feature = {
    id: slugify(featureName),
    name: featureName,
    status: 'candidate',
    journeys: journeys.map((item) => item.name),
    pages: pages.map((item) => item.name),
    business_rules: businessRules,
    analytics_requirements: analyticsRequirements,
    i18n_requirements: i18nRequirements,
    states,
    industry_signals: industrySignals,
    evidence_refs: [evidence('prd', toPosix(path.relative(repoRoot, absolutePrd)), 'PRD text was parsed into a candidate product contract.')],
  };

  return makeArtifact({
    schemaVersion: 'product-contract.v1',
    artifactId: 'product-contract',
    sourceInputs: [sourceInputFromFile('prd', absolutePrd, repoRoot)],
    body: {
      feature_count: 1,
      features: [feature],
      pages,
      journeys,
      extraction_notes: [
        'Script output is candidate structure only; semantic confirmation belongs to LLM experts.',
      ],
      degraded_modes: [],
    },
  });
}

function splitSections(text) {
  const sections = [];
  let current = { heading: 'root', lines: [] };
  for (const rawLine of text.split(/\r?\n/)) {
    const heading = rawLine.match(/^(#{1,6})\s+(.+?)\s*$/);
    if (heading) {
      sections.push(current);
      current = { heading: heading[2].trim(), lines: [] };
    } else {
      current.lines.push(rawLine);
    }
  }
  sections.push(current);
  return sections.filter((section) => section.heading !== 'root' || section.lines.join('').trim().length > 0);
}

function firstHeading(sections) {
  const section = sections.find((entry) => entry.heading && entry.heading !== 'root');
  return section ? section.heading : '';
}

function extractPages(lines, filePath, repoRoot) {
  const pages = [];
  for (const line of lines) {
    const pageMatch = line.match(/(?:页面|Page|Screen|Route)[:：\s-]+([A-Za-z0-9_\-\u4e00-\u9fa5 /]+)/i);
    const quoted = line.match(/([A-Za-z][A-Za-z0-9_]*(?:Screen|Page|Route))/);
    const name = redactForArtifactText(pageMatch ? pageMatch[1].trim() : (quoted ? quoted[1] : ''), { maxLength: 120 });
    if (!name) continue;
    pages.push({
      id: slugify(name),
      name,
      status: 'candidate',
      evidence: [evidence('prd', toPosix(path.relative(repoRoot, filePath)), `PRD mentions page candidate: ${name}`, {
        line_hash: hashText(line),
      })],
    });
  }
  return dedupeByName(pages);
}

function extractJourneys(lines, filePath, repoRoot) {
  const journeys = [];
  for (const line of lines) {
    if (!/(流程|旅程|journey|flow|->|→)/i.test(line)) continue;
    const steps = line
      .replace(/^[-*]\s*/, '')
      .split(/->|→|，|,|;|；/)
      .map((step) => redactForArtifactText(step.replace(/^(流程|旅程|journey|flow)[:：\s-]*/i, '').trim(), { maxLength: 120 }))
      .filter(Boolean);
    if (steps.length === 0) continue;
    const name = steps.length > 1 ? steps.join(' -> ') : steps[0];
    journeys.push({
      id: slugify(name),
      name,
      steps,
      status: 'candidate',
      evidence: [evidence('prd', toPosix(path.relative(repoRoot, filePath)), `PRD mentions journey candidate: ${name}`, {
        line_hash: hashText(line),
      })],
    });
  }
  return dedupeByName(journeys);
}

function extractBusinessRules(lines, filePath, repoRoot) {
  const patterns = /(必须|应当|应该|需要|不得|禁止|不能|require|must|should|shall|cannot|never)/i;
  return lines
    .filter((line) => patterns.test(line))
    .map((line, index) => ({
      id: `rule_${index + 1}`,
      description: redactForArtifactText(line.replace(/^[-*]\s*/, ''), { maxLength: 240 }),
      status: 'candidate',
      evidence: [evidence('prd', toPosix(path.relative(repoRoot, filePath)), redactForArtifactText(line.replace(/^[-*]\s*/, ''), { maxLength: 240 }), {
        line_hash: hashText(line),
      })],
    }));
}

function extractAnalyticsRequirements(lines, filePath, repoRoot) {
  const events = [];
  for (const line of lines) {
    const matches = line.match(/[a-z][a-z0-9_]*(?:_(?:view|click|submit|success|failed|failure|exposure|show))\b/gi) || [];
    for (const name of matches) {
      events.push({
        name,
        kind: classifyEventKind(name),
        status: 'candidate',
        evidence: [evidence('prd', toPosix(path.relative(repoRoot, filePath)), `PRD mentions analytics event: ${name}`)],
      });
    }
  }
  return dedupeByName(events);
}

function extractI18nRequirements(lines, filePath, repoRoot) {
  const keys = [];
  for (const line of lines) {
    const matches = line.match(/[a-z][a-z0-9_]*(?:_(?:title|button|label|message|error|reason|hint))\b/gi) || [];
    for (const name of matches) {
      keys.push({
        key: name,
        status: 'candidate',
        evidence: [evidence('prd', toPosix(path.relative(repoRoot, filePath)), `PRD mentions i18n key candidate: ${name}`)],
      });
    }
  }
  return unique(keys.map((entry) => entry.key)).map((key) => keys.find((entry) => entry.key === key));
}

function extractIndustrySignals(text) {
  const dictionaries = [
    ['securities', /(股票|证券|行情|买入|卖出|持仓|委托|撤单|trade|quote|position|order)/i],
    ['ecommerce', /(商品|购物车|下单|订单|支付|退款|物流|sku|cart|checkout|payment|refund)/i],
  ];
  return dictionaries
    .filter(([, pattern]) => pattern.test(text))
    .map(([industry]) => industry);
}

function dedupeByName(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = item.name || item.key || item.id;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractPrdContract(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractPrdContract,
  splitSections,
};
