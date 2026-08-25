#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const {
  hashFile,
  parseCommonArgs,
  publicPath,
  resolvePathAgainstRoot,
  resolveRepoRoot,
  unavailableSourceInput,
  writeJsonOutput,
} = require('./lib/audit-utils');
const { validateArtifact } = require('./validate-artifacts');

function buildAuditContext(options = {}) {
  if (!options.artifactsDir && !options.runDir) {
    throw new Error('artifacts_dir_required: build-audit-context requires --artifacts-dir or run-dir:<path>.');
  }
  const repoRoot = resolveRepoRoot(options);
  const artifactsDir = resolvePathAgainstRoot(repoRoot, options.artifactsDir || options.runDir);
  const outputPath = options.output ? resolvePathAgainstRoot(repoRoot, options.output) : null;
  const files = listJsonFiles(artifactsDir, {
    excludeDirs: new Set(['input', 'writeback-preview']),
    exclude: new Set([
      outputPath,
      path.join(artifactsDir, 'app-audit-context.json'),
      path.join(artifactsDir, 'artifact-manifest.json'),
      path.join(artifactsDir, 'latest-summary.json'),
    ].filter(Boolean).map((filePath) => path.resolve(filePath))),
  });
  const artifacts = [];
  const validation = [];

  for (const filePath of files) {
    let artifact = null;
    try {
      artifact = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    } catch (error) {
      validation.push({
        file: toPosix(path.relative(artifactsDir, filePath)),
        valid: false,
        errors: [{ path: 'file', code: 'invalid_json', message: error.message }],
      });
      continue;
    }

    const result = validateArtifact(artifact);
    validation.push({
      file: toPosix(path.relative(artifactsDir, filePath)),
      valid: result.valid,
      errors: result.errors,
    });
    artifacts.push({
      artifact_id: artifact.artifact_id || null,
      schema_version: artifact.schema_version || null,
      contract_status: artifact.contract_status || null,
      generated_at: artifact.generated_at || null,
      source_inputs: artifact.source_inputs || [],
      consumers: artifact.consumers || [],
      file: toPosix(path.relative(artifactsDir, filePath)),
    });
  }

  const context = {
    schema_version: 'spec-app-consistency-audit-context.v1',
    artifact_id: 'app-audit-context',
    generated_at: new Date().toISOString(),
    source_inputs: artifacts.length > 0
      ? artifacts.map((entry) => ({
        type: 'run_artifact',
        path: entry.file,
        source_hash: hashFile(path.join(artifactsDir, entry.file)),
        freshness: 'current-run',
      }))
      : [unavailableSourceInput('run_artifacts', publicPath(repoRoot, artifactsDir, 'run-outside-repo'), 'no_json_artifacts')],
    consumers: ['llm-audit-planner', 'expert-agents', 'report-writer'],
    contract_status: 'candidate',
    data_sensitivity: 'internal',
    artifacts_dir: publicPath(repoRoot, artifactsDir, 'run-outside-repo'),
    artifact_count: artifacts.length,
    valid: validation.every((entry) => entry.valid),
    artifacts,
    validation,
    degraded_modes: validation
      .filter((entry) => !entry.valid)
      .map((entry) => ({
        code: 'invalid_artifact',
        severity: 'error',
        summary: `Artifact failed validation: ${entry.file}`,
        path: entry.file,
      })),
  };

  return context;
}

function listJsonFiles(dirPath, options = {}) {
  if (!fs.existsSync(dirPath)) return [];
  const entries = fs.readdirSync(dirPath, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      if (!options.excludeDirs || !options.excludeDirs.has(entry.name)) {
        files.push(...listJsonFiles(fullPath, options));
      }
    } else if (entry.isFile() && entry.name.endsWith('.json')) {
      const absolutePath = path.resolve(fullPath);
      if (!options.exclude || !options.exclude.has(absolutePath)) files.push(fullPath);
    }
  }
  return files.sort((left, right) => left.localeCompare(right));
}

function toPosix(value) {
  return String(value || '').replace(/\\/g, '/');
}

function parseArgs(argv) {
  const options = parseCommonArgs(argv);
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--artifacts-dir') options.artifactsDir = argv[++index];
  }
  return options;
}

if (require.main === module) {
  try {
    const options = parseArgs(process.argv.slice(2));
    const context = buildAuditContext(options);
    const outputPath = options.output ? resolvePathAgainstRoot(resolveRepoRoot(options), options.output) : null;
    writeJsonOutput(context, outputPath, options);
    if (!context.valid) process.exitCode = 1;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  buildAuditContext,
};
