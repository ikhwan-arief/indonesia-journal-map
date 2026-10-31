import test from 'node:test';
import assert from 'node:assert/strict';
import { bubbleRadius } from '../web/assets/metrics.js';

test('bubble area uses a square-root scale with limits', () => {
  assert.equal(bubbleRadius(0, 100), 5);
  assert.equal(bubbleRadius(100, 100), 35);
  assert.ok(Math.abs(bubbleRadius(25, 100) - 20) < 1e-9);
  assert.equal(bubbleRadius(10_000, 100), 35);
});
