import test from 'node:test';
import assert from 'node:assert/strict';
import { parseTarget, hasActiveFilter, normalizeRecords, assertTableStructure } from './reader.mjs';

test('only HTTPS Feishu table links and complete target parameters', () => {
  assert.throws(() => parseTarget('https://evil.test/base/x?table=t&view=v'));
  assert.throws(() => parseTarget('https://a.feishu.cn/base/x?table=t'));
  assert.equal(parseTarget('https://a.feishu.cn/base/x?table=t&view=v').tableId, 't');
});
test('empty-value predicates are still filters; unknown shapes fail closed', () => {
  assert.equal(hasActiveFilter({ property: { filterInfo: { conditions: [{ operator: 'isEmpty', value: null }] } } }), true);
  assert.equal(hasActiveFilter({ property: { filterInfo: { conditions: [] } } }), false);
  assert.equal(hasActiveFilter({ property: { filterInfo: { newFormat: true } } }), true);
});
test('private endpoint requires map and explicit count/revision metadata', () => {
  assert.throws(() => normalizeRecords({ records: [] }));
  assert.equal(normalizeRecords({ recordMap: {}, hasMore: false }).hasMore, false);
  assert.throws(() => assertTableStructure({ meta: {}, viewMap: {}, fieldMap: {} }, { viewId: 'v' }));
  const table = { meta: { recordsNum: 2, rev: 1 }, viewMap: { v: { property: { fields: [] } } }, fieldMap: {} };
  assert.equal(assertTableStructure(table, { viewId: 'v' }).expectedRecords, 2);
});
