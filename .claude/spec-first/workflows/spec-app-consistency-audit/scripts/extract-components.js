#!/usr/bin/env node
'use strict';

const {
  classifyStates,
  evidence,
  listSourceTextFiles,
  makeArtifact,
  parseCommonArgs,
  readArtifact,
  readText,
  relativeTo,
  slugify,
  sourceInputFromFiles,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractComponents(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot, files } = scan;
  const figma = options.figmaContract ? readArtifact(options.figmaContract) : null;
  const codeComponents = files.flatMap((filePath) => inspectComponentFile(filePath, repoRoot));
  const interactionCandidates = files.flatMap((filePath) => inspectInteractionFile(filePath, repoRoot));
  const figmaComponents = figma ? figma.components || [] : [];
  const mappings = buildComponentMappings(figmaComponents, codeComponents);

  return makeArtifact({
    schemaVersion: 'component-contract.v1',
    artifactId: 'component-contract',
    sourceInputs: [
      sourceInputFromFiles('code', files, repoRoot, scan),
      ...(figma ? figma.source_inputs || [] : []),
    ],
    body: {
      code_components: codeComponents,
      figma_components: figmaComponents.map((component) => ({
        node_id: component.node_id || null,
        name: component.name || component.raw_label || null,
        label_hash: component.label_hash || null,
        raw_label_omitted: component.raw_label_omitted === true,
        variants: component.variants || [],
        status: 'candidate',
      })),
      component_mappings: mappings,
      interaction_candidates: interactionCandidates,
      extraction_notes: [
        'Component and interaction facts are candidate evidence for design-system, reuse, and mobile UX experts.',
      ],
      degraded_modes: scan.degraded_modes,
    },
  });
}

function inspectComponentFile(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  const text = readText(filePath);
  const names = [];
  const patterns = [
    /\b(?:fun|class|object|struct)\s+([A-Z][A-Za-z0-9_]*(?:Button|Input|Card|Dialog|Sheet|Toast|Component|Field|List|Row))/g,
    /@Composable\s+fun\s+([A-Z][A-Za-z0-9_]*)/g,
  ];
  for (const pattern of patterns) {
    let match = pattern.exec(text);
    while (match) {
      names.push(match[1]);
      match = pattern.exec(text);
    }
  }
  return unique(names).map((name) => ({
    id: slugify(name),
    name,
    file: rel,
    status: 'candidate',
    component_kind: inferComponentKind(name, rel),
    variants: classifyStates(text),
    props: extractProps(text),
    evidence: [evidence('code', rel, `Component candidate detected: ${name}`)],
  }));
}

function inspectInteractionFile(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  const text = readText(filePath);
  const candidates = [];
  const stateChecks = [
    ['keyboard_sensitive_form', /TextField|TextInput|Keyboard|FocusRequester|input/i, 'Input or keyboard-sensitive UI signal detected.'],
    ['submit_loading_or_disabled', /Button|submit|confirm|提交|确认/i, 'Submit-like UI path should be checked for loading, disabled, and duplicate guard states.'],
    ['list_empty_error_retry', /LazyColumn|RecyclerView|List|Paging|列表/i, 'List-like UI path should be checked for empty, error, and retry states.'],
    ['permission_denied_state', /Permission|permission|权限|denied/i, 'Permission path should include denied and settings guidance states.'],
    ['safe_area_sensitive_layout', /WindowInsets|SafeArea|statusBars|navigationBars|安全区/i, 'Safe-area sensitive layout signal detected.'],
  ];
  for (const [type, pattern, summary] of stateChecks) {
    if (pattern.test(text)) {
      candidates.push({
        type,
        file: rel,
        status: 'candidate',
        states_present: classifyStates(text),
        needs_semantic_review: true,
        evidence: [evidence('code', rel, summary)],
      });
    }
  }
  return candidates;
}

function buildComponentMappings(figmaComponents, codeComponents) {
  return figmaComponents.map((figmaComponent) => {
    const match = figmaComponent.name
      ? codeComponents.find((component) => normalize(component.name).includes(normalize(figmaComponent.name))
        || normalize(figmaComponent.name).includes(normalize(component.name)))
      : null;
    return {
      figma_component: figmaComponent.node_id || figmaComponent.id || figmaComponent.label_hash || null,
      code_component: match ? match.name : null,
      status: match ? 'candidate_match' : 'missing_code_candidate',
      evidence: [
        ...(figmaComponent.evidence || []),
        ...(match ? match.evidence || [] : []),
      ],
    };
  });
}

function inferComponentKind(name, rel) {
  if (/design-system|designsystem|ui-kit|uikit/i.test(rel)) return 'design_system';
  if (/Button|Input|Card|Dialog|Sheet|Toast|Field|List|Row/.test(name)) return 'ui_component';
  return 'business_component';
}

function extractProps(text) {
  const params = [];
  const match = text.match(/\(([^)]{0,500})\)/s);
  if (!match) return params;
  for (const part of match[1].split(',')) {
    const name = part.trim().match(/^([A-Za-z_][A-Za-z0-9_]*)\s*:/);
    if (name) params.push(name[1]);
  }
  return unique(params);
}

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]/g, '');
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractComponents(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractComponents,
  inspectInteractionFile,
};
