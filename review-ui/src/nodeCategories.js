export const NODE_TYPE_CATEGORIES = [
  ['Planning', 'square', ['Project', 'Goal', 'Task']],
  ['Architecture', 'triangle', ['ADR', 'DomainConcept', 'Service', 'Constraint', 'Risk']],
  ['Code and infrastructure', 'circle', ['Repository', 'Module', 'Class', 'Function', 'SourceFile', 'ApiEndpoint', 'DatabaseCollection', 'TestSuite']],
  ['Context', 'diamond', ['Observation', 'ConversationSummary']],
];
export const CATEGORIZED_TYPES = new Set(NODE_TYPE_CATEGORIES.flatMap(([, , members]) => members));

const shapeByType = new Map(NODE_TYPE_CATEGORIES.flatMap(([, shape, members]) =>
  members.map((type) => [type, shape])
));

export function nodeShape(type) {
  return shapeByType.get(type) ?? 'hexagon';
}
