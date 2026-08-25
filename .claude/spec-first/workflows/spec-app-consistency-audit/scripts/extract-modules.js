#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const {
  evidence,
  listSourceTextFiles,
  makeArtifact,
  parseCommonArgs,
  readText,
  relativeTo,
  slugify,
  sourceInputFromFiles,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractModules(options = {}) {
  const scan = listSourceTextFiles(options);
  const { sourceRoot, repoRoot, files } = scan;
  const settingsFiles = files.filter((filePath) => /settings\.gradle(\.kts)?$/.test(filePath));
  const buildFiles = files.filter((filePath) => /build\.gradle(\.kts)?$/.test(filePath));
  const modules = detectModules(sourceRoot, repoRoot, settingsFiles, buildFiles);
  const dependencies = detectModuleDependencies(repoRoot, buildFiles);
  const dependencyMetrics = calculateDependencyMetrics(modules, dependencies);

  return makeArtifact({
    schemaVersion: 'module-contract.v1',
    artifactId: 'module-contract',
    sourceInputs: [sourceInputFromFiles('code', [...settingsFiles, ...buildFiles], repoRoot, scan.truncated ? scan : {})],
    body: {
      modules,
      dependencies,
      module_count: modules.length,
      dependency_count: dependencies.length,
      dependency_cycles: detectDependencyCycles(dependencies),
      dependency_metrics: dependencyMetrics,
      boundary_candidates: detectModuleBoundaryCandidates(dependencies),
      extraction_notes: [
        'Module graph is a static candidate derived from Gradle settings and project dependencies.',
      ],
      degraded_modes: [
        ...scan.degraded_modes,
        ...(settingsFiles.length === 0 ? [{
          code: 'gradle_settings_missing',
          severity: 'info',
          summary: 'settings.gradle(.kts) was not found; module graph may be incomplete.',
          path: null,
        }] : []),
      ],
    },
  });
}

function detectModules(sourceRoot, repoRoot, settingsFiles, buildFiles) {
  const names = [];
  for (const filePath of settingsFiles) {
    const text = readText(filePath);
    const matches = text.matchAll(/include\s*\(([^)]*)\)|include\s+([^\n]+)/g);
    for (const match of matches) {
      const raw = match[1] || match[2] || '';
      const moduleMatches = raw.match(/["'](:[^"']+)["']/g) || [];
      for (const moduleMatch of moduleMatches) names.push(moduleMatch.replace(/["']/g, ''));
    }
  }
  for (const filePath of buildFiles) {
    const rel = relativeTo(repoRoot, filePath);
    const parts = rel.split('/');
    if (parts.length > 1) names.push(`:${parts.slice(0, -1).join(':')}`);
  }
  return unique(names).map((name) => {
    const dir = moduleNameToDir(sourceRoot, name);
    return {
      id: slugify(name),
      name,
      path: fs.existsSync(dir) ? relativeTo(repoRoot, dir) : null,
      kind: classifyModule(name),
      status: 'candidate',
      evidence: [evidence('code', settingsFiles[0] ? relativeTo(repoRoot, settingsFiles[0]) : null, `Gradle module candidate: ${name}`)],
    };
  });
}

function detectModuleDependencies(repoRoot, buildFiles) {
  const dependencies = [];
  for (const filePath of buildFiles) {
    const rel = relativeTo(repoRoot, filePath);
    const from = `:${rel.split('/').slice(0, -1).join(':')}`;
    const text = readText(filePath);
    const matches = text.matchAll(/project\s*\(\s*["'](:[^"']+)["']\s*\)/g);
    for (const match of matches) {
      dependencies.push({
        from,
        to: match[1],
        status: 'candidate',
        evidence: [evidence('code', rel, `${from} depends on ${match[1]}`)],
      });
    }
  }
  return dependencies;
}

function detectModuleBoundaryCandidates(dependencies) {
  const candidates = [];
  const metrics = calculateDependencyMetrics([], dependencies);
  for (const dependency of dependencies) {
    const fromKind = classifyModule(dependency.from);
    const toKind = classifyModule(dependency.to);
    if (fromKind === 'core' && toKind === 'feature') {
      candidates.push(boundaryCandidate('core_depends_on_feature', dependency));
    }
    if (fromKind === 'feature' && toKind === 'feature') {
      candidates.push(boundaryCandidate('feature_depends_on_feature', dependency));
    }
    const fromMetrics = metrics.find((entry) => entry.module === dependency.from);
    const toMetrics = metrics.find((entry) => entry.module === dependency.to);
    if (fromMetrics && toMetrics && fromMetrics.instability < 0.35 && toMetrics.instability > 0.65) {
      candidates.push(boundaryCandidate('stable_module_depends_on_unstable_module', dependency));
    }
  }
  return candidates;
}

function calculateDependencyMetrics(modules, dependencies) {
  const names = unique([
    ...modules.map((moduleEntry) => moduleEntry.name),
    ...dependencies.flatMap((dependency) => [dependency.from, dependency.to]),
  ]);
  return names.map((name) => {
    const fanIn = dependencies.filter((dependency) => dependency.to === name).length;
    const fanOut = dependencies.filter((dependency) => dependency.from === name).length;
    return {
      module: name,
      fan_in: fanIn,
      fan_out: fanOut,
      instability: fanIn + fanOut === 0 ? 0 : Number((fanOut / (fanIn + fanOut)).toFixed(2)),
      status: 'candidate',
    };
  });
}

function detectDependencyCycles(dependencies) {
  const graph = dependencies.reduce((map, dependency) => {
    if (!map.has(dependency.from)) map.set(dependency.from, []);
    map.get(dependency.from).push(dependency.to);
    return map;
  }, new Map());
  const cycles = [];
  for (const start of graph.keys()) {
    collectCycles(graph, start, start, [], cycles);
  }
  return dedupeCycles(cycles).map((cycle) => ({
    modules: cycle,
    status: 'candidate',
    needs_semantic_review: true,
    evidence: cycle.slice(0, -1).map((from, index) => evidence('code', null, `${from} depends on ${cycle[index + 1]}`)),
  }));
}

function collectCycles(graph, start, current, pathStack, cycles) {
  const nextPath = [...pathStack, current];
  for (const next of graph.get(current) || []) {
    if (next === start) {
      cycles.push([...nextPath, start]);
      continue;
    }
    if (!nextPath.includes(next)) collectCycles(graph, start, next, nextPath, cycles);
  }
}

function dedupeCycles(cycles) {
  const seen = new Set();
  return cycles.filter((cycle) => {
    const body = cycle.slice(0, -1);
    const rotations = body.map((_, index) => [...body.slice(index), ...body.slice(0, index)].join('>'));
    const key = rotations.sort()[0];
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function classifyModule(name) {
  const lower = String(name || '').toLowerCase();
  if (/design|uikit|component/.test(lower)) return 'design-system';
  if (/analytics|tracking|telemetry/.test(lower)) return 'analytics';
  if (/i18n|l10n|locale|string/.test(lower)) return 'i18n';
  if (/core|common|shared/.test(lower)) return 'core';
  if (/feature/.test(lower)) return 'feature';
  if (/app|android|ios/.test(lower)) return 'app';
  return 'module';
}

function moduleNameToDir(sourceRoot, name) {
  return path.join(sourceRoot, String(name).replace(/^:/, '').replace(/:/g, '/'));
}

function boundaryCandidate(type, dependency) {
  return {
    type,
    from: dependency.from,
    to: dependency.to,
    status: 'candidate',
    needs_semantic_review: true,
    evidence: dependency.evidence,
  };
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractModules(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  calculateDependencyMetrics,
  detectDependencyCycles,
  detectModuleBoundaryCandidates,
  extractModules,
};
