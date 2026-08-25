#!/usr/bin/env node
'use strict';

const {
  evidence,
  listSourceTextFiles,
  makeArtifact,
  parseCommonArgs,
  readText,
  relativeTo,
  sourceInputFromFiles,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractI18n(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot, files } = scan;
  const stringResources = files.flatMap((filePath) => inspectStringResource(filePath, repoRoot));
  const hardcodedTextCandidates = files.flatMap((filePath) => inspectHardcodedText(filePath, repoRoot));
  const placeholderCandidates = stringResources.filter((entry) => /%[0-9$]*[sdif@]/.test(entry.value));
  const pluralResources = stringResources.filter((entry) => entry.kind === 'plural');

  return makeArtifact({
    schemaVersion: 'i18n-contract.v1',
    artifactId: 'i18n-contract',
    sourceInputs: [sourceInputFromFiles('code', files, repoRoot, scan)],
    body: {
      string_resource_count: stringResources.length,
      string_resources: stringResources,
      hardcoded_text_candidates: hardcodedTextCandidates,
      placeholder_candidates: placeholderCandidates,
      plural_resources: pluralResources,
      locale_risk_candidates: buildLocaleRiskCandidates(stringResources, hardcodedTextCandidates),
      extraction_notes: [
        'I18n extraction reports resource and hardcoded-text candidates. User-visible text intent requires expert review.',
      ],
      degraded_modes: [
        ...scan.degraded_modes,
        ...(stringResources.length === 0 ? [{
          code: 'i18n_resources_missing',
          severity: 'warning',
          summary: 'No strings.xml or Localizable.strings resources were detected.',
          path: null,
        }] : []),
      ],
    },
  });
}

function inspectStringResource(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  if (!/(strings\.xml|Localizable\.strings)$/i.test(filePath)) return [];
  const text = readText(filePath);
  const resources = [];
  if (/strings\.xml$/i.test(filePath)) {
    const stringPattern = /<string\s+name="([^"]+)">([\s\S]*?)<\/string>/g;
    let match = stringPattern.exec(text);
    while (match) {
      resources.push(resource('android_string', match[1], cleanXml(match[2]), rel));
      match = stringPattern.exec(text);
    }
    const pluralPattern = /<plurals\s+name="([^"]+)">([\s\S]*?)<\/plurals>/g;
    let pluralMatch = pluralPattern.exec(text);
    while (pluralMatch) {
      resources.push(resource('plural', pluralMatch[1], cleanXml(pluralMatch[2]), rel));
      pluralMatch = pluralPattern.exec(text);
    }
  } else {
    const iosPattern = /"([^"]+)"\s*=\s*"([^"]*)";/g;
    let match = iosPattern.exec(text);
    while (match) {
      resources.push(resource('ios_string', match[1], match[2], rel));
      match = iosPattern.exec(text);
    }
  }
  return resources;
}

function inspectHardcodedText(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  if (!/\.(kt|kts|java|swift|m|mm)$/i.test(filePath)) return [];
  const text = readText(filePath);
  const candidates = [];
  const stringPattern = /"([^"\n]{2,80})"/g;
  let match = stringPattern.exec(text);
  while (match) {
    const value = match[1];
    if (isLikelyUserVisibleText(value)) {
      candidates.push({
        text: value,
        file: rel,
        status: 'candidate',
        suggested_key: suggestKey(value),
        evidence: [evidence('i18n', rel, `Hardcoded user-visible text candidate: ${value}`)],
      });
    }
    match = stringPattern.exec(text);
  }
  return candidates.slice(0, 200);
}

function buildLocaleRiskCandidates(resources, hardcodedTexts) {
  const risks = [];
  if (hardcodedTexts.length > 0) {
    risks.push({
      type: 'hardcoded_text',
      count: hardcodedTexts.length,
      status: 'candidate',
      evidence: hardcodedTexts.slice(0, 10).flatMap((entry) => entry.evidence || []),
    });
  }
  const placeholderKeys = resources.filter((entry) => /%[0-9$]*[sdif@]/.test(entry.value)).map((entry) => entry.key);
  if (placeholderKeys.length > 0) {
    risks.push({
      type: 'placeholder_consistency_needs_review',
      keys: unique(placeholderKeys),
      status: 'candidate',
      evidence: resources.filter((entry) => placeholderKeys.includes(entry.key)).flatMap((entry) => entry.evidence || []),
    });
  }
  return risks;
}

function resource(kind, key, value, file) {
  return {
    kind,
    key,
    value,
    file,
    status: 'candidate',
    placeholder_count: (value.match(/%[0-9$]*[sdif@]/g) || []).length,
    evidence: [evidence('i18n', file, `I18n resource candidate: ${key}`)],
  };
}

function isLikelyUserVisibleText(value) {
  if (/^[a-z0-9_./:-]+$/i.test(value)) return false;
  if (/^https?:\/\//.test(value)) return false;
  return /[\u4e00-\u9fa5]|[A-Za-z]{3,}\s+[A-Za-z]{2,}/.test(value);
}

function suggestKey(value) {
  const ascii = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return ascii || `text_${Buffer.from(value).toString('hex').slice(0, 8)}`;
}

function cleanXml(value) {
  return String(value || '').replace(/<[^>]+>/g, '').trim();
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractI18n(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractI18n,
  inspectHardcodedText,
  inspectStringResource,
};
