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
  publicPath,
  readJson,
  redactForArtifactText,
  resolveBoundedInputPath,
  slugify,
  sourceInputFromFile,
  unavailableSourceInput,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractFigmaContract(options = {}) {
  const contextPath = options.figmaContext;
  const redaction = normalizeRedaction(options.redaction || 'internal');
  if (!contextPath) {
    return makeArtifact({
      schemaVersion: 'figma-design-contract.v1',
      artifactId: 'figma-design-contract',
      sourceInputs: [unavailableSourceInput('figma', 'figma-context', 'figma_context_missing')],
      body: {
        screens: [],
        components: [],
        raw_label_policy: redaction,
        figma_context_mode: options.figmaNode || options.figmaFile ? 'mcp_reference_only' : 'none',
        degraded_modes: [{
          code: options.figmaNode || options.figmaFile ? 'figma_materialized_context_missing' : 'figma_context_missing',
          severity: 'warning',
          summary: options.figmaNode || options.figmaFile
            ? 'Figma reference was provided, but host MCP context was not materialized to a local JSON file.'
            : 'Figma context file was not provided; design conclusions stay out of scope.',
          path: null,
        }],
      },
    });
  }

  const repoRoot = path.resolve(options.repoRoot || options.source || process.cwd());
  const resolution = resolveBoundedInputPath({
    repoRoot,
    inputPath: contextPath,
    kind: 'figma',
    expected: 'file',
    allowOutside: options.allowOutside || options.allowOutsidePaths || [],
  });
  if (!resolution.ok) {
    throw new Error(`figma context rejected: ${resolution.reason}`);
  }
  const absolutePath = resolution.realpath;
  const context = readJson(absolutePath);
  const nodes = normalizeFigmaNodes(context);
  const screens = nodes
    .filter((node) => isScreenNode(node))
    .map((node) => normalizeScreen(node, absolutePath, repoRoot, redaction));
  const components = nodes
    .filter((node) => isComponentNode(node))
    .map((node) => normalizeComponent(node, absolutePath, repoRoot, redaction));

  return makeArtifact({
    schemaVersion: 'figma-design-contract.v1',
    artifactId: 'figma-design-contract',
    sourceInputs: [sourceInputFromFile('figma', absolutePath, repoRoot)],
    body: {
      raw_label_policy: redaction,
      figma_context_mode: 'materialized_json',
      screen_count: screens.length,
      component_count: components.length,
      screens,
      components,
      extraction_notes: [
        'Figma context is treated as host-provided untrusted input and normalized without live MCP traversal.',
        'Default internal redaction preserves short screen/component labels for cross-source matching while hashing all labels.',
      ],
      degraded_modes: [],
    },
  });
}

function normalizeFigmaNodes(context) {
  if (Array.isArray(context)) return context;
  if (Array.isArray(context.nodes)) return context.nodes;
  if (Array.isArray(context.frames)) return context.frames;
  if (context.document) return flattenNode(context.document);
  return [];
}

function flattenNode(node) {
  const result = [];
  if (!node || typeof node !== 'object') return result;
  result.push(node);
  const children = Array.isArray(node.children) ? node.children : [];
  for (const child of children) result.push(...flattenNode(child));
  return result;
}

function isScreenNode(node) {
  const type = String(node.type || '').toUpperCase();
  const name = String(node.name || '');
  return type === 'FRAME' || /(Screen|Page|Route|页面|首页|详情|确认|结果)/.test(name);
}

function isComponentNode(node) {
  const type = String(node.type || '').toUpperCase();
  const name = String(node.name || '');
  return type.includes('COMPONENT') || /(Button|Input|Card|Dialog|Sheet|Toast|组件)/.test(name);
}

function normalizeScreen(node, filePath, repoRoot, redaction = 'internal') {
  const textEntries = collectTextEntries(node);
  const text = `${node.name || ''}\n${textEntries.map((entry) => entry.value).join('\n')}`;
  const interactionNodes = collectInteractionNodes(node).map((entry) => ({
    type: entry.type,
    node_id: entry.node_id,
    ...redactedLabelFields(entry.name, redaction),
    label_hash: hashText(entry.name),
    suggested_analytics_event: `figma_${slugify(entry.node_id || hashText(entry.name).slice(7, 15))}_${entry.type === 'button' ? 'click' : 'view'}`,
    event_kind: classifyEventKind(`${entry.type}_${hashText(entry.name).slice(7, 15)}`),
  }));
  const components = collectComponentRefs(node, redaction);
  const labelFields = redactedLabelFields(node.name || node.id || 'UnnamedFrame', redaction);

  return {
    id: slugify(node.id || 'figma-screen'),
    node_id: node.id || null,
    ...(labelFields.name ? { name: labelFields.name } : {}),
    ...labelFields,
    label_hash: hashText(node.name || node.id || 'UnnamedFrame'),
    redaction_level: redaction,
    status: 'candidate',
    states: classifyStates(text),
    components,
    texts: textEntries.map((entry) => ({
      node_id: entry.node_id,
      kind: 'text_node',
      character_count: entry.value.length,
      ...redactedTextFields(entry.value, redaction),
      text_hash: hashText(entry.value),
      suggested_i18n_key: suggestTextKey(entry),
      evidence_summary: redaction === 'strict'
        ? 'Figma text node candidate; raw text is omitted under strict redaction.'
        : 'Figma text node candidate; short non-sensitive text may be retained for consistency matching.',
    })),
    interaction_nodes: interactionNodes,
    evidence: [evidence('figma', publicPath(repoRoot, filePath, 'figma-outside-repo'), 'Figma frame candidate extracted from materialized context.', {
      node: node.id || null,
    })],
  };
}

function normalizeComponent(node, filePath, repoRoot, redaction = 'internal') {
  const text = `${node.name || ''}\n${collectTextEntries(node).map((entry) => entry.value).join('\n')}`;
  const labelFields = redactedLabelFields(node.name || node.id || 'UnnamedComponent', redaction);
  return {
    id: slugify(node.id || 'figma-component'),
    node_id: node.id || null,
    ...(labelFields.name ? { name: labelFields.name } : {}),
    ...labelFields,
    label_hash: hashText(node.name || node.id || 'UnnamedComponent'),
    redaction_level: redaction,
    status: 'candidate',
    variants: classifyStates(text),
    evidence: [evidence('figma', publicPath(repoRoot, filePath, 'figma-outside-repo'), 'Figma component candidate extracted from materialized context.', {
      node: node.id || null,
    })],
  };
}

function collectTextEntries(node) {
  const entries = [];
  visit(node, (entry) => {
    if (typeof entry.characters === 'string' && entry.characters.trim()) {
      entries.push({
        node_id: entry.id || null,
        value: entry.characters.trim(),
      });
    } else if (String(entry.type || '').toUpperCase() === 'TEXT' && entry.name) {
      entries.push({
        node_id: entry.id || null,
        value: String(entry.name).trim(),
      });
    }
  });
  return uniqueTextEntries(entries).slice(0, 100);
}

function uniqueTextEntries(entries) {
  const seen = new Set();
  return entries.filter((entry) => {
    const key = `${entry.node_id || ''}:${hashText(entry.value)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function suggestTextKey(entry) {
  if (entry.node_id) return `figma_text_${slugify(entry.node_id)}`;
  return `figma_text_${hashText(entry.value).slice(7, 15)}`;
}

function collectInteractionNodes(node) {
  const nodes = [];
  visit(node, (entry) => {
    const name = String(entry.name || '');
    if (/(button|btn|submit|confirm|cancel|按钮|提交|确认|取消)/i.test(name)) {
      nodes.push({ type: 'button', name, node_id: entry.id || null });
    } else if (/(input|field|textfield|输入)/i.test(name)) {
      nodes.push({ type: 'input', name, node_id: entry.id || null });
    }
  });
  return nodes;
}

function collectComponentRefs(node, redaction = 'internal') {
  const refs = [];
  visit(node, (entry) => {
    const name = String(entry.name || '');
    if (/(Button|Input|Card|Dialog|Sheet|Toast|组件)/.test(name)) {
      refs.push({
        node_id: entry.id || null,
        ...redactedLabelFields(name, redaction),
        label_hash: hashText(name),
      });
    }
  });
  const seen = new Set();
  return refs.filter((entry) => {
    const key = `${entry.node_id || ''}:${entry.label_hash}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function normalizeRedaction(value) {
  const redaction = String(value || 'internal').toLowerCase();
  if (['strict', 'internal', 'none'].includes(redaction)) return redaction;
  return 'internal';
}

function redactedLabelFields(value, redaction) {
  const label = String(value || '').trim();
  if (redaction === 'strict' || !label || isSensitiveArtifactText(label)) {
    return {
      raw_label_omitted: true,
    };
  }
  if (redaction === 'none') {
    return {
      name: label,
      raw_label: label,
      raw_label_omitted: false,
    };
  }
  if (shouldRetainShortLabel(label)) {
    return {
      name: label,
      raw_label: label,
      raw_label_omitted: false,
    };
  }
  return {
    raw_label_omitted: true,
  };
}

function redactedTextFields(value, redaction) {
  const text = String(value || '').trim();
  if (redaction === 'strict' || !text || isSensitiveArtifactText(text)) {
    return {
      raw_text_omitted: true,
    };
  }
  if (redaction === 'none' || shouldRetainShortLabel(text)) {
    return {
      text,
      raw_text_omitted: false,
    };
  }
  return {
    raw_text_omitted: true,
  };
}

function shouldRetainShortLabel(value) {
  const text = String(value || '').trim();
  if (text.length === 0 || text.length > 80) return false;
  if (isSensitiveArtifactText(text)) return false;
  if (/(password|passwd|token|secret|credential|phone|email|身份证|密码|手机号|邮箱|银行卡|证件)/i.test(text)) {
    return false;
  }
  return true;
}

function isSensitiveArtifactText(value) {
  const text = String(value || '').trim();
  if (!text) return false;
  return redactForArtifactText(text, { maxLength: Math.max(text.length + 32, 120) }) !== text;
}

function visit(node, callback) {
  if (!node || typeof node !== 'object') return;
  callback(node);
  const children = Array.isArray(node.children) ? node.children : [];
  for (const child of children) visit(child, callback);
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractFigmaContract(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractFigmaContract,
  normalizeRedaction,
  normalizeFigmaNodes,
};
