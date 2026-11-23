import { Crosshair, X } from 'lucide-react';
import { useEffect, useId, useMemo, useRef } from 'react';
import {
  Background, BaseEdge, Controls, Handle, MarkerType, Position, ReactFlow, ViewportPortal,
  useOptionsStore, useReactFlow, useViewportStore,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { SimilarRecords } from './SimilarRecords.jsx';
import { DetailLink } from './Details.jsx';
import { nodeShape } from './nodeCategories.js';

const RING_GAP = 130;
const NODE_CLEARANCE = 120;
const EMPTY = [];
const ORIGIN = [0.5, 0.5];
const EDGE_COLOR = '#587783';
const SELECTED_EDGE_COLOR = '#164a60';

const NODE_COLORS = {
  Project: '#0f766e',
  Goal: '#a16207',
  Task: '#0369a1',
  ADR: '#7c3aed',
  DomainConcept: '#4338ca',
  Service: '#1d4ed8',
  Repository: '#047857',
  Module: '#0e7490',
  Class: '#6d28d9',
  Function: '#be185d',
  SourceFile: '#2563eb',
  ApiEndpoint: '#b45309',
  DatabaseCollection: '#334155',
  TestSuite: '#0f766e',
  Constraint: '#c2410c',
  Risk: '#be123c',
  Observation: '#475569',
  ConversationSummary: '#a21caf',
};

export function NodeMarker({ type }) {
  const shape = nodeShape(type);
  const color = NODE_COLORS[type] ?? '#475569';
  return <span aria-hidden="true" className={`node-marker ${shape}`} style={{ '--node-color': color }} />;
}

function GraphNode({ data }) {
  const { entity, depth, selected, onSelect } = data;
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} className="graph-handle" />
      <button type="button" className="viewer-node nodrag nopan" aria-pressed={selected} data-centered={depth === 0}
        onClick={(event) => onSelect(entity, event.currentTarget)}
        title={`${entity.entity_type} · ${entity.source_path || entity.name} · ${depth === 0 ? 'Graph center' : `${depth} ${depth === 1 ? 'hop' : 'hops'}`}`}>
        <NodeMarker type={entity.entity_type} />
        <span className="viewer-node-name">{entity.name}</span>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} className="graph-handle" />
    </>
  );
}

function GraphEdge({ id, sourceX, sourceY, targetX, targetY, markerEnd, data }) {
  const angle = Math.atan2(targetY - sourceY, targetX - sourceX);
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  const bendX = (sourceX + targetX) / 2 - dy * data.offset;
  const bendY = (sourceY + targetY) / 2 + dx * data.offset;
  const path = `M ${sourceX + 12 * dx} ${sourceY + 12 * dy} Q ${bendX} ${bendY} ${targetX - 12 * dx} ${targetY - 12 * dy}`;
  const color = data.selected ? SELECTED_EDGE_COLOR : EDGE_COLOR;
  function select(event) {
    event.stopPropagation();
    data.onSelect(id, event.currentTarget);
  }
  return <>
    <BaseEdge id={id} markerEnd={markerEnd} interactionWidth={0}
      style={{ stroke: color, strokeWidth: data.selected ? 2.8 : 1.4 }} path={path} />
    <path className="graph-edge-hit nopan" d={path} role="button" tabIndex={0}
      aria-label={data.label} aria-pressed={data.selected} onClick={select}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          select(event);
        }
      }} />
  </>;
}

const NODE_TYPES = { architecture: GraphNode };
const EDGE_TYPES = { architecture: GraphEdge };

function CenterViewport({ radius }) {
  const { setCenter } = useReactFlow();
  const width = useViewportStore((state) => state.width);
  const height = useViewportStore((state) => state.height);
  const ready = useOptionsStore((state) => Boolean(state.panZoom));
  useEffect(() => {
    if (!ready || !width || !height) return;
    const zoom = Math.min(1, (width - 24) / (2 * radius + 210), (height - 24) / (2 * radius + 54));
    setCenter(0, 0, { zoom: Math.max(0.1, zoom) });
  }, [width, height, radius, ready, setCenter]);
  return null;
}

function layout(graph, relationships, hiddenTypes) {
  const nodes = new Map([[graph.center.key, { entity: graph.center, depth: 0 }]]);
  const edges = new Map();
  const inverse = new Map(Object.entries(relationships).map(([kind, back]) => [back, kind]));
  for (const path of graph.paths) {
    path.nodes.forEach((entity, depth) => {
      if (hiddenTypes.includes(entity.entity_type)) return;
      const previous = nodes.get(entity.key);
      if (!previous || depth < previous.depth) nodes.set(entity.key, { entity, depth });
    });
    for (const edge of path.relationships) {
      let { source_key: from, target_key: to, relationship_type: kind } = edge;
      if (inverse.has(kind) && !(kind in relationships)) {
        [from, to] = [to, from];
        kind = inverse.get(kind);
      }
      if (kind === 'related_to' && from > to) [from, to] = [to, from];
      edges.set(JSON.stringify([from, to, kind, edge.source]), { from, to, kind, edge });
    }
  }

  for (const [key, { from, to }] of edges) {
    if (!nodes.has(from) || !nodes.has(to)) edges.delete(key);
  }

  const layers = new Map();
  for (const node of nodes.values()) {
    if (node.depth === 0) continue;
    if (!layers.has(node.depth)) layers.set(node.depth, []);
    layers.get(node.depth).push(node);
  }
  const rings = [];
  let radius = 0;
  for (const [depth, layer] of [...layers].sort(([a], [b]) => a - b)) {
    const spacing = layer.length > 1 ? NODE_CLEARANCE / (2 * Math.sin(Math.PI / layer.length)) : 0;
    radius = Math.max(radius + RING_GAP, depth * RING_GAP, spacing);
    rings.push({ radius, depth, layer });
  }
  Object.assign(nodes.get(graph.center.key), { x: 0, y: 0 });
  for (const { radius, depth, layer } of rings) {
    layer.sort((a, b) => a.entity.name.localeCompare(b.entity.name) || a.entity.key.localeCompare(b.entity.key));
    layer.forEach((node, index) => {
      const angle = -Math.PI / 2 + (depth - 1) * Math.PI / 4 + index * 2 * Math.PI / layer.length;
      node.x = Math.cos(angle) * radius;
      node.y = Math.sin(angle) * radius;
    });
  }
  return { nodes, edges, rings, radius };
}

function NodeDetails({ entity, centered, onClose, onFocus, navigate, token, onUnauthorized }) {
  const facts = [
    ['Role', entity.role],
    ['Repository', entity.repository],
    ['Source path', entity.source_path],
    ['Qualified name', entity.qualified_name],
    ['HTTP method', entity.method],
    ['Route', entity.path],
    ['Status', entity.status],
    ['Index status', entity.index_state],
    ['Memory record', entity.memory_key],
    ['Scope', entity.scope],
    ['Authority', entity.authority],
    ['Enforcement', entity.enforcement_status],
    ['Source', entity.source],
    ['Confidence', entity.confidence],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');

  return (
    <DetailsPanel title={entity.name} closeLabel="Close node details" onClose={onClose}
      category={<><NodeMarker type={entity.entity_type} /> {entity.entity_type}</>}>
      {(entity.summary || entity.rule || entity.description) && <p className="graph-node-description">{entity.summary || entity.rule || entity.description}</p>}
      {facts.length > 0 && <dl className="graph-node-facts">
        {facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      </dl>}
      {entity.memory_key && <SimilarRecords recordKey={entity.memory_key}
        token={token} navigate={navigate} onUnauthorized={onUnauthorized} />}
      <footer className="graph-node-actions">
        {!centered && <button type="button" className="button outlined"
          onClick={() => onFocus(entity.key)}><Crosshair size={16} aria-hidden="true" /> Center graph</button>}
        <DetailLink destination={entity.virtual ? 'records' : 'entities'} entityKey={entity.virtual ? entity.memory_key : entity.key} navigate={navigate}>Open full details</DetailLink>
      </footer>
    </DetailsPanel>
  );
}

function DetailsPanel({ title, category, closeLabel, onClose, children }) {
  const heading = useId();
  const closeButton = useRef(null);
  useEffect(() => { closeButton.current?.focus({ preventScroll: true }); }, []);
  return (
    <aside className="graph-node-details" aria-labelledby={heading} onKeyDown={(event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    }}>
      <header className="graph-node-details-heading">
        <div><p className="graph-kind">{category}</p><h3 id={heading}>{title}</h3></div>
        <button type="button" className="button icon outlined" aria-label={closeLabel} ref={closeButton} onClick={onClose}>
          <X size={18} aria-hidden="true" />
        </button>
      </header>
      {children}
    </aside>
  );
}

function EdgeDetails({ relationship, onClose, navigate }) {
  const { from, to, kind, edge } = relationship;
  const symmetric = kind === 'related_to';
  return (
    <DetailsPanel title={kind} category="Relationship" closeLabel="Close relationship details" onClose={onClose}>
      <dl className="graph-node-facts">
        <div><dt>Direction</dt><dd>{symmetric ? 'Bidirectional' : 'From → To'}</dd></div>
        <div><dt>{symmetric ? 'Node 1' : 'From'}</dt><dd><DetailLink destination="entities" entityKey={from.key} navigate={navigate}>{from.name}</DetailLink> <small>({from.entity_type})</small></dd></div>
        <div><dt>{symmetric ? 'Node 2' : 'To'}</dt><dd><DetailLink destination="entities" entityKey={to.key} navigate={navigate}>{to.name}</DetailLink> <small>({to.entity_type})</small></dd></div>
        <div><dt>Source</dt><dd>{edge.source}</dd></div>
        <div><dt>Confidence</dt><dd>{edge.confidence}</dd></div>
      </dl>
    </DetailsPanel>
  );
}

export function GraphDiagram({ graph, relationships, hiddenTypes = EMPTY, selected, onSelect, onSelectEdge, onClose, onFocus, navigate, token, onUnauthorized }) {
  const diagram = useMemo(() => layout(graph, relationships, hiddenTypes), [graph, relationships, hiddenTypes]);
  const nodes = [...diagram.nodes.values()].map(({ entity, depth, x, y }) => ({
    id: entity.key,
    type: 'architecture',
    position: { x, y },
    data: { entity, depth, selected: selected?.node?.key === entity.key, onSelect },
  }));
  const parallelEdges = new Map();
  for (const [id, { from, to }] of diagram.edges) {
    const pair = JSON.stringify([from, to].sort());
    if (!parallelEdges.has(pair)) parallelEdges.set(pair, []);
    parallelEdges.get(pair).push(id);
  }
  const edges = [...parallelEdges.values()].flatMap((ids) => ids.map((id, index) => {
    const { from, to, kind } = diagram.edges.get(id);
    const selectedEdge = selected?.edgeId === id;
    const fromName = diagram.nodes.get(from).entity.name;
    const toName = diagram.nodes.get(to).entity.name;
    return {
      id, source: from, target: to, type: 'architecture',
      markerEnd: kind === 'related_to' ? undefined
        : { type: MarkerType.ArrowClosed, color: selectedEdge ? SELECTED_EDGE_COLOR : EDGE_COLOR },
      data: {
        selected: selectedEdge, onSelect: onSelectEdge,
        offset: (index - (ids.length - 1) / 2) * 40 * (from < to ? 1 : -1),
        label: `${fromName} ${kind.replaceAll('_', ' ')} ${toName}`,
      },
    };
  }));
  const activeEdge = diagram.edges.get(selected?.edgeId);

  return (
    <>
      <div className="diagram-toolbar">
        <p>{diagram.nodes.size} {diagram.nodes.size === 1 ? 'node' : 'nodes'} · {diagram.edges.size} {diagram.edges.size === 1 ? 'edge' : 'edges'}</p>
        <DetailLink destination={graph.center.virtual ? 'records' : 'entities'}
          entityKey={graph.center.virtual ? graph.center.memory_key : graph.center.key} navigate={navigate}>Open center node</DetailLink>
      </div>
      <div className="diagram-viewer" role="region" aria-label="Interactive node and edge diagram"
        onKeyDown={(event) => { if (event.key === 'Escape' && selected) onClose(); }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={NODE_TYPES} edgeTypes={EDGE_TYPES}
          nodeOrigin={ORIGIN} minZoom={0.1} maxZoom={3} nodesDraggable={false} nodesConnectable={false}
          nodesFocusable={false} edgesFocusable={false} elementsSelectable={false} zoomOnDoubleClick={false}>
          <CenterViewport radius={diagram.radius} />
          <Background gap={20} color="#cddde5" />
          <ViewportPortal>
            {diagram.rings.map(({ radius, depth }) => (
              <span key={depth} aria-hidden="true" className="viewer-ring"
                style={{ width: radius * 2, height: radius * 2, left: -radius, top: -radius }} />
            ))}
          </ViewportPortal>
          <Controls showInteractive={false} />
        </ReactFlow>
        {selected?.node && <NodeDetails key={selected.node.key} entity={selected.node}
          centered={selected.node.key === graph.center.key} onClose={onClose} onFocus={onFocus}
          navigate={navigate} token={token} onUnauthorized={onUnauthorized} />}
        {activeEdge && <EdgeDetails key={selected.edgeId} relationship={{ ...activeEdge,
          from: diagram.nodes.get(activeEdge.from).entity, to: diagram.nodes.get(activeEdge.to).entity,
        }} onClose={onClose} navigate={navigate} />}
      </div>
      {diagram.edges.size === 0 && <p className="muted">{graph.center.virtual ? 'This record has no linked graph node.' : 'No relationships match this view.'}</p>}
      {graph.truncated && diagram.nodes.size > 1 && <p role="status">Showing {graph.paths.length} paths sampled by hop. Narrow the type or hop filters to explore a smaller neighborhood.</p>}
      {diagram.edges.size > 0 && (
        <details className="diagram-edges">
          <summary>Relationship details</summary>
          <ul>{[...diagram.edges].map(([key, { from, to, kind, edge }]) => (
            <li key={key}><strong>{diagram.nodes.get(from).entity.name}</strong>{' '}
              <button type="button" className="graph-edge-list-button" aria-pressed={selected?.edgeId === key}
                onClick={(event) => onSelectEdge(key, event.currentTarget)}><code>{kind}</code></button>{' '}<strong>{diagram.nodes.get(to).entity.name}</strong>
              <small>{edge.source} · Confidence {edge.confidence}</small>
            </li>
          ))}</ul>
        </details>
      )}
    </>
  );
}
