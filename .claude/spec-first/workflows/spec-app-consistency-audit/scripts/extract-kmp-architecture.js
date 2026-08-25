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

function extractKmpArchitecture(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot, files } = scan;
  const sourceSets = detectSourceSets(files, repoRoot);
  const layers = detectLayers(files, repoRoot);
  const expectActual = detectExpectActual(files, repoRoot);
  const sourceImports = extractSourceImports(files, repoRoot);
  const boundaryCandidates = detectBoundaryCandidates(files, repoRoot);

  return makeArtifact({
    schemaVersion: 'kmp-architecture-contract.v1',
    artifactId: 'kmp-architecture-contract',
    sourceInputs: [sourceInputFromFiles('code', files, repoRoot, scan)],
    body: {
      source_sets: sourceSets,
      layers,
      source_imports: sourceImports,
      expect_actual: expectActual,
      boundary_candidates: boundaryCandidates,
      architecture_candidates: inferArchitectureCandidates(sourceSets, layers),
      extraction_notes: [
        'Boundary entries are static candidates. LLM experts must confirm intent, dependency direction, and platform abstractions.',
      ],
      degraded_modes: scan.degraded_modes,
    },
  });
}

function detectSourceSets(files, repoRoot) {
  const sourceSets = [
    ['commonMain', /(^|\/)commonMain\//],
    ['androidMain', /(^|\/)androidMain\//],
    ['iosMain', /(^|\/)iosMain\//],
    ['commonTest', /(^|\/)commonTest\//],
    ['androidTest', /(^|\/)androidTest\//],
    ['iosTest', /(^|\/)iosTest\//],
  ];
  return sourceSets.map(([name, pattern]) => {
    const matched = files.filter((filePath) => pattern.test(relativeTo(repoRoot, filePath)));
    return {
      name,
      present: matched.length > 0,
      file_count: matched.length,
      sample_files: matched.slice(0, 10).map((filePath) => relativeTo(repoRoot, filePath)),
    };
  });
}

function detectLayers(files, repoRoot) {
  const layerNames = ['domain', 'data', 'presentation', 'ui', 'platform', 'repository', 'usecase'];
  return layerNames.map((name) => {
    const matched = files.filter((filePath) => new RegExp(`(^|/)${name}s?(/|$)`, 'i').test(relativeTo(repoRoot, filePath)));
    return {
      name,
      file_count: matched.length,
      sample_files: matched.slice(0, 10).map((filePath) => relativeTo(repoRoot, filePath)),
    };
  }).filter((entry) => entry.file_count > 0);
}

function detectExpectActual(files, repoRoot) {
  const expects = [];
  const actuals = [];
  for (const filePath of files) {
    const rel = relativeTo(repoRoot, filePath);
    const text = readText(filePath);
    if (/\bexpect\s+(?:class|object|interface|fun|val|var)\b/.test(text)) expects.push({ file: rel });
    if (/\bactual\s+(?:class|object|interface|fun|val|var)\b/.test(text)) actuals.push({ file: rel });
  }
  return {
    expect_count: expects.length,
    actual_count: actuals.length,
    expects,
    actuals,
    platform_symmetry_candidate: expects.length === 0 ? 'no_expect_symbols' : (actuals.length >= expects.length ? 'has_actuals' : 'missing_actual_candidates'),
  };
}

function detectBoundaryCandidates(files, repoRoot) {
  const candidates = [];
  for (const filePath of files) {
    const rel = relativeTo(repoRoot, filePath);
    const text = readText(filePath);
    const imports = parseImports(text);
    if (/commonMain\//.test(rel) && imports.some((entry) => /^(android|androidx|platform\.UIKit|platform\.Foundation|java\.io)\b/.test(entry))) {
      candidates.push(candidate('platform_import_in_common_main', rel, 'commonMain file imports platform-specific APIs.'));
    }
    if (/(^|\/)domain\//i.test(rel) && imports.some((entry) => /(?:ui|compose|androidx|swiftui)/i.test(entry))) {
      candidates.push(candidate('domain_depends_on_ui', rel, 'Domain layer appears to import UI/platform presentation APIs.'));
    }
    if (/(^|\/)(ui|presentation)\//i.test(rel) && /RepositoryImpl|Dao|Retrofit|HttpClient|SqlDriver/.test(text)) {
      candidates.push(candidate('ui_touches_data_implementation', rel, 'UI or presentation layer references data implementation details.'));
    }
    if (/UseCase/.test(rel) && !/(Result|sealed|Either|Success|Failure)/.test(text)) {
      candidates.push(candidate('usecase_without_structured_result_signal', rel, 'UseCase lacks obvious structured success/failure result signal.'));
    }
  }
  return candidates;
}

function extractSourceImports(files, repoRoot) {
  return files
    .filter((filePath) => /\.(kt|kts|swift|java)$/i.test(filePath))
    .map((filePath) => {
      const rel = relativeTo(repoRoot, filePath);
      const imports = parseImports(readText(filePath));
      return {
        file: rel,
        source_set: sourceSetForFile(rel),
        layer: layerForFile(rel),
        imports,
        import_count: imports.length,
        status: 'candidate',
      };
    })
    .filter((entry) => entry.import_count > 0)
    .slice(0, 500);
}

function parseImports(text) {
  const imports = [];
  const pattern = /^\s*import\s+([A-Za-z0-9_.*.`]+(?:\.[A-Za-z0-9_.*.`]+)*)/gm;
  let match = pattern.exec(text);
  while (match) {
    imports.push(match[1].replace(/`/g, ''));
    match = pattern.exec(text);
  }
  return unique(imports);
}

function sourceSetForFile(rel) {
  const match = rel.match(/(^|\/)(commonMain|androidMain|iosMain|commonTest|androidTest|iosTest)\//);
  return match ? match[2] : 'unknown';
}

function layerForFile(rel) {
  const match = rel.match(/(^|\/)(domain|data|presentation|ui|platform|repository|usecases?)\//i);
  return match ? match[2].toLowerCase() : 'unknown';
}

function inferArchitectureCandidates(sourceSets, layers) {
  const result = [];
  if (sourceSets.find((entry) => entry.name === 'commonMain' && entry.present)) result.push('kmp');
  if (layers.some((entry) => ['domain', 'data', 'presentation'].includes(entry.name))) result.push('clean-architecture');
  if (layers.some((entry) => entry.name === 'usecase')) result.push('usecase-driven');
  return unique(result);
}

function candidate(type, file, summary) {
  return {
    type,
    file,
    summary,
    status: 'candidate',
    needs_semantic_review: true,
    evidence: [evidence('architecture', file, summary)],
  };
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractKmpArchitecture(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  detectBoundaryCandidates,
  extractSourceImports,
  extractKmpArchitecture,
  parseImports,
};
