#!/usr/bin/env node
'use strict';

const {
  classifyEventKind,
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

function extractAnalytics(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot, files } = scan;
  const events = files.flatMap((filePath) => inspectAnalyticsFile(filePath, repoRoot));
  const keyPathCoverage = summarizeKeyPathCoverage(events);

  return makeArtifact({
    schemaVersion: 'analytics-contract.v1',
    artifactId: 'analytics-contract',
    sourceInputs: [sourceInputFromFiles('code', files, repoRoot, scan)],
    body: {
      event_count: events.length,
      events,
      key_path_coverage: keyPathCoverage,
      extraction_notes: [
        'Analytics extraction identifies event-name and parameter candidates only; policy completeness belongs to expert review.',
      ],
      degraded_modes: [
        ...scan.degraded_modes,
        ...(events.length === 0 ? [{
          code: 'analytics_events_missing',
          severity: 'warning',
          summary: 'No analytics event calls were detected.',
          path: null,
        }] : []),
      ],
    },
  });
}

function inspectAnalyticsFile(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  const text = readText(filePath);
  const events = [];
  const patterns = [
    /(?:track|trackEvent|logEvent|analytics\.[A-Za-z0-9_]+)\s*\(\s*["']([^"']+)["']/g,
    /eventName\s*=\s*["']([^"']+)["']/g,
    /["']([a-z][a-z0-9_]*(?:_(?:view|click|submit|success|failed|failure|exposure|show)))["']/g,
  ];
  for (const pattern of patterns) {
    let match = pattern.exec(text);
    while (match) {
      events.push(eventFact(match[1], rel, text));
      match = pattern.exec(text);
    }
  }
  return dedupeEvents(events);
}

function eventFact(name, file, text) {
  return {
    name,
    kind: classifyEventKind(name),
    file,
    status: 'candidate',
    params: extractParamsNearEvent(text, name),
    has_failure_reason: /failure[_-]?reason|error[_-]?code|error[_-]?message|reason/.test(text),
    evidence: [evidence('analytics', file, `Analytics event candidate: ${name}`)],
  };
}

function extractParamsNearEvent(text, eventName) {
  const index = text.indexOf(eventName);
  if (index < 0) return [];
  const window = text.slice(index, index + 800);
  const params = [];
  const patterns = [
    /["']([a-z][a-z0-9_]*(?:_id|_type|_reason|_source|_status|_code|amount|symbol|platform))["']\s*(?:to|:|=)/gi,
    /([a-z][a-z0-9_]*(?:Id|Type|Reason|Source|Status|Code|Amount|Symbol|Platform))\s*=/g,
  ];
  for (const pattern of patterns) {
    let match = pattern.exec(window);
    while (match) {
      params.push(match[1]);
      match = pattern.exec(window);
    }
  }
  return unique(params);
}

function summarizeKeyPathCoverage(events) {
  const kinds = new Set(events.map((event) => event.kind));
  return {
    has_page_view: kinds.has('page_view'),
    has_click: kinds.has('click'),
    has_submit: kinds.has('submit'),
    has_success: kinds.has('success'),
    has_failed: kinds.has('failed'),
    missing_kinds: ['page_view', 'click', 'submit', 'success', 'failed'].filter((kind) => !kinds.has(kind)),
  };
}

function dedupeEvents(events) {
  const seen = new Set();
  return events.filter((event) => {
    const key = `${event.file}:${event.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractAnalytics(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractAnalytics,
  inspectAnalyticsFile,
  summarizeKeyPathCoverage,
};
