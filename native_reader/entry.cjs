'use strict';
// Private stdin/stdout adapter; credentials stay inside this process.
const { initialize } = require('./native-reader.cjs');
const { createReaderService } = require('./checkdup.cjs');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 8192) process.exit(2); });
process.stdin.on('end', async () => {
  try {
    const payload = JSON.parse(input);
    const { native, reader } = initialize();
    const result = await createReaderService(native, reader).load(payload);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stdout.write(JSON.stringify({ success: false, error: error.message }));
    process.exitCode = 1;
  }
});
