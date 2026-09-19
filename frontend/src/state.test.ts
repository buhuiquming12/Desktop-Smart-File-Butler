import { parseApproval, reduceThreadEvent } from './state';
import type { WSEvent } from './types';

const event = (thread_id: string, type: WSEvent['type'], payload: Record<string, unknown>): WSEvent => ({ thread_id, type, payload });

const first = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('a', 'task', { task_id: 'same', title: 'A', status: 'running' })).view;
const second = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('b', 'task', { task_id: 'same', title: 'B', status: 'success' })).view;
if (first.tasks[0]?.title !== 'A' || second.tasks[0]?.title !== 'B') throw new Error('thread state leaked');
if (parseApproval(event('a', 'approval_required', { approval_id: 'x', action: 'delete', target: 'a.txt' }))?.thread_id !== 'a') throw new Error('approval thread missing');
