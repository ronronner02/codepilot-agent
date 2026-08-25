#!/usr/bin/env node
'use strict';

const path = require('node:path');

const { extractCodeContract } = require('./extract-code-contract');
const { extractFigmaContract } = require('./extract-figma-contract');
const { extractPrdContract } = require('./extract-prd-contract');
const {
  evidence,
  makeArtifact,
  normalizeName,
  parseCommonArgs,
  readArtifact,
  slugify,
  toPosix,
  unavailableSourceInput,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractPageRoutes(options = {}) {
  const repoRoot = path.resolve(options.repoRoot || options.source || '.');
  const product = options.productContract
    ? readArtifact(options.productContract)
    : (options.prd ? extractPrdContract(options) : null);
  const figma = options.figmaContract
    ? readArtifact(options.figmaContract)
    : (options.figmaContext ? extractFigmaContract(options) : null);
  const code = options.codeContract
    ? readArtifact(options.codeContract)
    : extractCodeContract(options);

  const sourceInputs = [
    ...(product ? product.source_inputs || [] : [unavailableSourceInput('prd', 'prd', 'prd_missing')]),
    ...(figma ? figma.source_inputs || [] : [unavailableSourceInput('figma', 'figma-context', 'figma_context_missing')]),
    ...(code ? code.source_inputs || [] : [unavailableSourceInput('code', '.', 'code_contract_missing')]),
  ];

  const productPages = product ? product.pages || [] : [];
  const figmaScreens = figma ? figma.screens || [] : [];
  const codeRoutes = code ? code.routes || [] : [];
  const codeScreens = code ? code.screens || [] : [];
  const routes = buildRouteContracts({
    productPages,
    figmaScreens,
    codeRoutes,
    codeScreens,
  });

  const coverage_gaps = buildCoverageGaps(productPages, figmaScreens, codeRoutes, codeScreens);

  return makeArtifact({
    schemaVersion: 'page-route-contract.v1',
    artifactId: 'page-route-contract',
    sourceInputs,
    body: {
      route_count: routes.length,
      routes,
      coverage_gaps,
      extraction_notes: [
        'Route trace links PRD, Figma, and code by normalized names and explicit route strings; missing links are candidates for expert review, not confirmed issues.',
      ],
      degraded_modes: [
        ...(!product ? [degraded('product_contract_missing', 'warning', 'Product contract is unavailable.')] : []),
        ...(!figma ? [degraded('figma_contract_missing', 'warning', 'Figma contract is unavailable.')] : []),
      ],
    },
  });
}

function buildRouteContracts(inputs) {
  const routeMap = new Map();
  for (const route of inputs.codeRoutes) {
    const key = normalizeName(route.name || route.path);
    const matchedScreen = route.screen || findNameMatch(route.name || route.path, inputs.codeScreens, 'name');
    const crossSourceName = matchedScreen || route.name || route.path;
    routeMap.set(key, {
      id: slugify(route.name || route.path),
      name: route.name || route.path,
      path: route.path || route.name,
      screen: matchedScreen,
      figma_frame: findNameMatch(crossSourceName, inputs.figmaScreens, 'name'),
      prd_page: findNameMatch(crossSourceName, inputs.productPages, 'name'),
      entry_points: [],
      required_params: route.required_params || [],
      guards: route.guards || [],
      back_behavior: 'candidate_needs_review',
      status: 'candidate',
      trace_status: 'candidate',
      evidence: route.evidence || [evidence('code', route.file || null, `Route candidate: ${route.path || route.name}`)],
    });
  }

  for (const page of inputs.productPages) {
    const key = normalizeName(page.name);
    if (!routeMap.has(key)) {
      routeMap.set(key, routeFromPage(page, inputs));
    }
  }

  for (const screen of inputs.figmaScreens) {
    const key = normalizeName(figmaScreenRef(screen));
    if (!routeMap.has(key)) {
      routeMap.set(key, routeFromFigma(screen, inputs));
    }
  }

  return [...routeMap.values()].map((route) => ({
    ...route,
    trace: {
      prd_page: route.prd_page || null,
      figma_screen: route.figma_frame || null,
      code_route: route.path || null,
      code_screen: route.screen || null,
    },
  }));
}

function routeFromPage(page, inputs) {
  return {
    id: slugify(page.name),
    name: `${page.name}Route`,
    path: null,
    screen: findNameMatch(page.name, inputs.codeScreens, 'name'),
    figma_frame: findNameMatch(page.name, inputs.figmaScreens, 'name'),
    prd_page: page.name,
    entry_points: [],
    required_params: [],
    guards: [],
    back_behavior: 'candidate_needs_review',
    status: 'candidate',
    trace_status: 'missing_code_route_candidate',
    evidence: page.evidence || [],
  };
}

function routeFromFigma(screen, inputs) {
  const screenRef = figmaScreenRef(screen);
  return {
    id: slugify(screenRef),
    name: `${screenRef}Route`,
    path: null,
    screen: screen.name ? findNameMatch(screen.name, inputs.codeScreens, 'name') : null,
    figma_frame: screenRef,
    prd_page: screen.name ? findNameMatch(screen.name, inputs.productPages, 'name') : null,
    entry_points: [],
    required_params: [],
    guards: [],
    back_behavior: 'candidate_needs_review',
    status: 'candidate',
    trace_status: 'missing_code_route_candidate',
    evidence: screen.evidence || [],
  };
}

function buildCoverageGaps(productPages, figmaScreens, codeRoutes, codeScreens) {
  const gaps = [];
  for (const page of productPages) {
    if (!findNameMatch(page.name, figmaScreens, 'name')) {
      gaps.push(gap('prd_page_without_figma_screen', page.name, page.evidence));
    }
    if (!findNameMatch(page.name, codeRoutes, 'name') && !findNameMatch(page.name, codeScreens, 'name')) {
      gaps.push(gap('prd_page_without_code_route', page.name, page.evidence));
    }
  }
  for (const screen of figmaScreens) {
    const screenName = screen.name || null;
    if (!screenName || (!findNameMatch(screenName, codeRoutes, 'name') && !findNameMatch(screenName, codeScreens, 'name'))) {
      gaps.push(gap('figma_screen_without_code_route', figmaScreenRef(screen), screen.evidence));
    }
  }
  return gaps;
}

function figmaScreenRef(screen) {
  return screen.name || screen.raw_label || screen.node_id || screen.id || screen.label_hash || 'figma-screen';
}

function findNameMatch(name, list, field) {
  const normalized = normalizeName(name);
  const item = list.find((entry) => {
    const value = normalizeName(entry[field] || entry.path || '');
    return value && (value.includes(normalized) || normalized.includes(value));
  });
  return item ? (item[field] || item.path) : null;
}

function gap(type, name, sourceEvidence) {
  return {
    type,
    name,
    status: 'candidate',
    evidence: sourceEvidence || [],
  };
}

function degraded(code, severity, summary) {
  return { code, severity, summary, path: null };
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractPageRoutes(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  buildCoverageGaps,
  buildRouteContracts,
  extractPageRoutes,
};
