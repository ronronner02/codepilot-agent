#!/usr/bin/env node
'use strict';

const {
  evidence,
  listSourceTextFiles,
  makeArtifact,
  parseCommonArgs,
  readArtifact,
  readText,
  relativeTo,
  sourceInputFromFiles,
  unavailableSourceInput,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

const INDUSTRY_DICTIONARY = {
  'finance-common': ['金融', '账户', '资产', '余额', '转账', '充值', '提现', '支付', '银行卡', '实名', 'kyc', 'account', 'balance', 'asset', 'transfer', 'withdraw', 'deposit'],
  securities: ['股票', '证券', '行情', '买入', '卖出', '持仓', '委托', '撤单', '风控', 'trade', 'quote', 'position', 'order', 'risk'],
  ecommerce: ['商品', '购物车', '下单', '订单', '支付', '退款', '物流', 'sku', 'cart', 'checkout', 'payment', 'refund'],
};

function buildIndustryProfile(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot } = scan;
  const artifactInputs = [
    options.productContract,
    options.figmaContract,
    options.codeContract,
    options.analyticsContract,
    options.i18nContract,
  ].filter(Boolean);
  const artifacts = artifactInputs.map((filePath) => readArtifact(filePath));
  const files = artifactInputs.length === 0 ? scan.files : [];
  const corpus = buildCorpus({ artifacts, files, repoRoot });
  const industryCandidates = Object.entries(INDUSTRY_DICTIONARY)
    .map(([industry, terms]) => scoreIndustry(industry, terms, corpus))
    .filter((candidate) => candidate.evidence.length > 0)
    .sort((left, right) => right.confidence - left.confidence);

  return makeArtifact({
    schemaVersion: 'industry-profile.v1',
    artifactId: 'industry-profile',
    sourceInputs: [
      ...artifacts.flatMap((artifact) => artifact.source_inputs || []),
      ...(files.length > 0 ? [sourceInputFromFiles('code', files, repoRoot, scan)] : []),
      ...(artifactInputs.length === 0 && files.length === 0 ? [unavailableSourceInput('artifacts', 'artifacts', 'industry_inputs_missing')] : []),
    ],
    body: {
      preview_only: true,
      requires_human_confirmation: true,
      industry_candidates: industryCandidates,
      selected_preview: industryCandidates[0] || null,
      extraction_notes: [
        'Industry profile is preview-only and must not write repo-profile or promote industry rules to confirmed issues.',
      ],
      degraded_modes: [
        ...scan.degraded_modes,
        ...(industryCandidates.length === 0 ? [{
          code: 'industry_confidence_low',
          severity: 'info',
          summary: 'No industry-specific evidence crossed the preview threshold.',
          path: null,
        }] : []),
      ],
    },
  });
}

function buildCorpus(inputs) {
  const entries = [];
  for (const artifact of inputs.artifacts) {
    entries.push({
      source: artifact.artifact_id || 'artifact',
      file: artifact.artifact_id || null,
      text: JSON.stringify(stripLargeFields(artifact)).toLowerCase(),
    });
  }
  for (const filePath of inputs.files) {
    entries.push({
      source: 'code',
      file: relativeTo(inputs.repoRoot, filePath),
      text: readText(filePath).toLowerCase(),
    });
  }
  return entries;
}

function scoreIndustry(industry, terms, corpus) {
  const evidenceItems = [];
  const seenTerms = new Set();
  const termsBySource = {};
  for (const entry of corpus) {
    for (const term of terms) {
      if (entry.text.includes(term.toLowerCase())) {
        seenTerms.add(term);
        termsBySource[entry.source] = termsBySource[entry.source] || new Set();
        termsBySource[entry.source].add(term);
        evidenceItems.push(evidence(entry.source, entry.file, `${industry} signal: ${term}`));
      }
    }
  }
  const confidenceBySource = buildConfidenceBySource(termsBySource, terms.length);
  const sourceCount = Object.values(confidenceBySource).filter((value) => value > 0).length;
  const rawConfidence = seenTerms.size / Math.max(4, terms.length * 0.5);
  const confidenceCap = sourceCount >= 2 ? 0.95 : 0.6;
  const confidence = Math.min(confidenceCap, Number(rawConfidence.toFixed(2)));
  return {
    industry,
    domain: industryDomain(industry),
    subdomain_candidates: [industry],
    feature_signals: [...seenTerms].slice(0, 20),
    confidence,
    confidence_by_source: confidenceBySource,
    evidence: uniqueEvidence(evidenceItems).slice(0, 20),
    recommended_rule_packs: [
      'common-app',
      'kmp-clean-architecture',
      'component-module-reuse',
      industry,
    ],
    advisory_only: true,
  };
}

function buildConfidenceBySource(termsBySource, termCount) {
  const sources = ['prd', 'product-contract', 'figma-design-contract', 'codebase-contract', 'analytics-contract', 'i18n-contract', 'code'];
  return sources.reduce((result, source) => {
    const matched = termsBySource[source] ? termsBySource[source].size : 0;
    const key = canonicalSource(source);
    const score = Math.min(0.6, Number((matched / Math.max(4, termCount * 0.5)).toFixed(2)));
    result[key] = Math.max(result[key] || 0, score);
    return result;
  }, {});
}

function canonicalSource(source) {
  if (source === 'product-contract') return 'prd';
  if (source === 'figma-design-contract') return 'figma';
  if (source === 'codebase-contract') return 'code';
  if (source === 'analytics-contract') return 'analytics';
  if (source === 'i18n-contract') return 'i18n';
  return source;
}

function industryDomain(industry) {
  if (industry === 'finance-common' || industry === 'securities') return 'finance';
  if (industry === 'ecommerce') return 'commerce';
  return 'unknown';
}

function stripLargeFields(artifact) {
  const copy = { ...artifact };
  delete copy.source_inputs;
  return copy;
}

function uniqueEvidence(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = `${item.source}:${item.file}:${item.summary}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(buildIndustryProfile(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  INDUSTRY_DICTIONARY,
  buildIndustryProfile,
  scoreIndustry,
};
