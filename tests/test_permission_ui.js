const fs = require('fs');
const path = require('path');
const vm = require('vm');

function extractFunction(source, name) {
    const start = source.indexOf(`function ${name}`);
    if (start === -1) {
        throw new Error(`Could not find function ${name}`);
    }
    let bodyStart = -1;
    let parenDepth = 0;
    let sawOpenParen = false;
    for (let i = start; i < source.length; i++) {
        const ch = source[i];
        if (ch === '(') {
            parenDepth += 1;
            sawOpenParen = true;
        } else if (ch === ')') {
            parenDepth -= 1;
        } else if (ch === '{' && sawOpenParen && parenDepth === 0) {
            bodyStart = i;
            break;
        }
    }
    if (bodyStart === -1) {
        throw new Error(`Could not find body for function ${name}`);
    }
    let braceDepth = 0;
    let end = -1;
    for (let i = bodyStart; i < source.length; i++) {
        const ch = source[i];
        if (ch === '{') {
            braceDepth += 1;
        } else if (ch === '}') {
            braceDepth -= 1;
            if (braceDepth === 0) {
                end = i + 1;
                break;
            }
        }
    }
    if (end === -1) {
        throw new Error(`Could not parse function ${name}`);
    }
    return source.slice(start, end);
}

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

const chatJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
const helperSource = extractFunction(chatJs, 'getPermissionOptionAction');
const getPermissionOptionAction = vm.runInNewContext(`(${helperSource})`);
const isStructuredPermissionSource = extractFunction(chatJs, 'isStructuredPermission');
const isStructuredPermission = vm.runInNewContext(`(${isStructuredPermissionSource})`);
const getPermissionToolClassSource = extractFunction(chatJs, 'getPermissionToolClass');
const getPermissionToolClass = vm.runInNewContext(`(${getPermissionToolClassSource})`);
const renderPermissionContentSource = extractFunction(chatJs, 'renderPermissionContent');
const renderPermissionContent = vm.runInNewContext(
    `(${renderPermissionContentSource})`,
    {
        escapeHtml(text) {
            return String(text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        },
        getPermissionOptionAction,
        isStructuredPermission,
        getPermissionToolClass,
    },
);

assert(getPermissionOptionAction({ label: 'Apply', key: 'y' }) === 'approve', 'expected Apply to approve');
assert(getPermissionOptionAction({ label: 'Skip', key: 'a' }) === 'deny', 'expected Skip to deny');
assert(getPermissionOptionAction({ label: 'Cancel', key: 'esc' }) === 'deny', 'expected esc to deny');
assert(getPermissionOptionAction({ label: 'Allow once', key: '1' }) === 'approve', 'expected Allow once to approve');
assert(getPermissionOptionAction({ label: 'No', key: '2' }) === 'deny', 'expected No to deny');
assert(isStructuredPermission({ source_kind: 'structured', tool_name: 'Bash', description: 'Run command' }) === true, 'expected structured permission to be detected');
assert(getPermissionToolClass('Bash') === 'bash', 'expected Bash tool class');
assert(getPermissionToolClass('Write') === 'write', 'expected Write tool class');
assert(getPermissionToolClass('"><script>alert(1)</script>') === 'other', 'expected unexpected tool name to normalize to other');

const structuredHtml = renderPermissionContent(
    {
        id: 'perm123',
        source_kind: 'structured',
        tool_name: 'Bash',
        request_id: 'abcde',
        description: 'Remove node_modules directory',
        input_preview: '{"command":"rm -rf node_modules"}',
        options: [
            { key: 'allow', label: 'Approve' },
            { key: 'deny', label: 'Deny' },
        ],
        status: 'pending',
    },
    true,
);

assert(structuredHtml.includes('permission-tool-badge'), 'expected structured card to render tool badge');
assert(structuredHtml.includes('permission-preview'), 'expected structured card to render preview block');
assert(structuredHtml.includes("respondToPermission('perm123', 'allow', 'approve')"), 'expected structured approve button');
assert(structuredHtml.includes('abcde'), 'expected structured card to render request id');
assert(structuredHtml.includes('permission-tool-bash'), 'expected safe Bash tool class');

const unexpectedToolHtml = renderPermissionContent(
    {
        id: 'perm999',
        source_kind: 'structured',
        tool_name: '"><script>alert(1)</script>',
        request_id: 'fghjk',
        description: 'Unexpected tool name',
        options: [
            { key: 'allow', label: 'Approve' },
            { key: 'deny', label: 'Deny' },
        ],
        status: 'pending',
    },
    true,
);

assert(unexpectedToolHtml.includes('permission-tool-other'), 'expected unexpected tool name to use safe other class');
assert(!unexpectedToolHtml.includes('permission-tool-"><script>alert(1)</script>'), 'expected raw tool name to never appear in class interpolation');

console.log('test_permission_ui: PASS');
