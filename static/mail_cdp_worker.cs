using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace TikFlowMailHelper
{
    public class LocalProxyForwarder : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly string _upHost;
        private readonly int _upPort;
        private readonly string _authHeader;
        private volatile bool _stopped;
        public int LocalPort { get; private set; }

        public LocalProxyForwarder(string host, int port, string user, string pass)
        {
            _upHost = host;
            _upPort = port;
            string raw = (user ?? "") + ":" + (pass ?? "");
            _authHeader = Convert.ToBase64String(Encoding.UTF8.GetBytes(raw));
            _listener = new TcpListener(IPAddress.Loopback, 0);
            _listener.Start();
            LocalPort = ((IPEndPoint)_listener.LocalEndpoint).Port;
            Task.Run(() => AcceptLoop());
        }

        private async Task AcceptLoop()
        {
            while (!_stopped)
            {
                try
                {
                    TcpClient client = await _listener.AcceptTcpClientAsync().ConfigureAwait(false);
                    var _ = Task.Run(() => HandleClient(client));
                }
                catch
                {
                    break;
                }
            }
        }

        private async Task HandleClient(TcpClient client)
        {
            using (client)
            {
                try
                {
                    NetworkStream cStream = client.GetStream();
                    byte[] headerBuf = new byte[8192];
                    int read = 0;
                    string headerStr = "";
                    while (read < headerBuf.Length)
                    {
                        int n = await cStream.ReadAsync(headerBuf, read, headerBuf.Length - read).ConfigureAwait(false);
                        if (n <= 0) return;
                        read += n;
                        headerStr = Encoding.ASCII.GetString(headerBuf, 0, read);
                        if (headerStr.Contains("\r\n\r\n")) break;
                    }
                    int sepIdx = headerStr.IndexOf("\r\n\r\n", StringComparison.Ordinal);
                    if (sepIdx < 0) return;

                    string headPart = headerStr.Substring(0, sepIdx);
                    int bodyOffset = sepIdx + 4;
                    int extraLen = read - bodyOffset;

                    string[] lines = headPart.Split(new[] { "\r\n" }, StringSplitOptions.None);
                    var outLines = new List<string>();
                    outLines.Add(lines[0]);
                    for (int i = 1; i < lines.Length; i++)
                    {
                        if (!lines[i].StartsWith("Proxy-Authorization:", StringComparison.OrdinalIgnoreCase))
                        {
                            outLines.Add(lines[i]);
                        }
                    }
                    outLines.Add("Proxy-Authorization: Basic " + _authHeader);
                    string rebuilt = string.Join("\r\n", outLines) + "\r\n\r\n";
                    byte[] outBytes = Encoding.ASCII.GetBytes(rebuilt);

                    using (var upstream = new TcpClient())
                    {
                        await upstream.ConnectAsync(_upHost, _upPort).ConfigureAwait(false);
                        NetworkStream uStream = upstream.GetStream();
                        await uStream.WriteAsync(outBytes, 0, outBytes.Length).ConfigureAwait(false);
                        if (extraLen > 0)
                        {
                            await uStream.WriteAsync(headerBuf, bodyOffset, extraLen).ConfigureAwait(false);
                        }
                        Task t1 = cStream.CopyToAsync(uStream);
                        Task t2 = uStream.CopyToAsync(cStream);
                        await Task.WhenAny(t1, t2).ConfigureAwait(false);
                    }
                }
                catch
                {
                }
            }
        }

        public void Dispose()
        {
            _stopped = true;
            try { _listener.Stop(); } catch { }
        }
    }

    public class CdpSession : IDisposable
    {
        private readonly ClientWebSocket _ws = new ClientWebSocket();
        private readonly ConcurrentDictionary<int, TaskCompletionSource<Dictionary<string, object>>> _pending =
            new ConcurrentDictionary<int, TaskCompletionSource<Dictionary<string, object>>>();
        private readonly SemaphoreSlim _sendLock = new SemaphoreSlim(1, 1);
        private readonly JavaScriptSerializer _json = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
        private int _nextId;
        public event Action<string, Dictionary<string, object>> OnEvent;
        private readonly ConcurrentQueue<Dictionary<string, object>> _events = new ConcurrentQueue<Dictionary<string, object>>();

        public string DrainEventsJson()
        {
            var rows = new List<Dictionary<string, object>>();
            Dictionary<string, object> row;
            while (rows.Count < 500 && _events.TryDequeue(out row)) rows.Add(row);
            return _json.Serialize(rows);
        }

        public async Task ConnectAsync(string wsUrl, int timeoutMs = 12000)
        {
            try { _ws.Options.Proxy = null; } catch { }
            using (var cts = new CancellationTokenSource(timeoutMs))
            {
                await _ws.ConnectAsync(new Uri(wsUrl), cts.Token).ConfigureAwait(false);
            }
            var _ = Task.Run(() => ReceiveLoop());
        }

        private async Task ReceiveLoop()
        {
            byte[] buf = new byte[65536];
            try
            {
                while (_ws.State == WebSocketState.Open)
                {
                    using (var ms = new MemoryStream())
                    {
                        WebSocketReceiveResult res;
                        do
                        {
                            res = await _ws.ReceiveAsync(new ArraySegment<byte>(buf), CancellationToken.None).ConfigureAwait(false);
                            if (res.MessageType == WebSocketMessageType.Close) return;
                            if (res.Count > 0) ms.Write(buf, 0, res.Count);
                        } while (!res.EndOfMessage);

                        string raw = Encoding.UTF8.GetString(ms.ToArray());
                        var msg = _json.Deserialize<Dictionary<string, object>>(raw);
                        if (msg == null) continue;

                        if (msg.ContainsKey("id") && msg["id"] != null)
                        {
                            int id = Convert.ToInt32(msg["id"]);
                            TaskCompletionSource<Dictionary<string, object>> tcs;
                            if (_pending.TryRemove(id, out tcs))
                            {
                                tcs.TrySetResult(msg);
                            }
                        }
                        else if (msg.ContainsKey("method"))
                        {
                            string m = Convert.ToString(msg["method"]);
                            var p = msg.ContainsKey("params") ? msg["params"] as Dictionary<string, object> : null;
                            _events.Enqueue(new Dictionary<string, object> { { "method", m }, { "params", p } });
                            var handler = OnEvent;
                            if (handler != null)
                            {
                                try { handler(m, p ?? new Dictionary<string, object>()); } catch { }
                            }
                        }
                    }
                }
            }
            catch
            {
            }
            finally
            {
                foreach (var kv in _pending)
                {
                    kv.Value.TrySetCanceled();
                }
            }
        }

        public async Task<Dictionary<string, object>> SendAsync(string method, object parameters = null, int timeoutMs = 15000)
        {
            int id = Interlocked.Increment(ref _nextId);
            var tcs = new TaskCompletionSource<Dictionary<string, object>>();
            _pending[id] = tcs;

            var payload = new Dictionary<string, object>
            {
                { "id", id },
                { "method", method },
                { "params", parameters ?? new Dictionary<string, object>() }
            };
            byte[] bytes = Encoding.UTF8.GetBytes(_json.Serialize(payload));

            await _sendLock.WaitAsync().ConfigureAwait(false);
            try
            {
                using (var cts = new CancellationTokenSource(timeoutMs))
                {
                    await _ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, cts.Token).ConfigureAwait(false);
                }
            }
            finally
            {
                _sendLock.Release();
            }

            using (var cts = new CancellationTokenSource(timeoutMs))
            {
                using (cts.Token.Register(() => tcs.TrySetException(new TimeoutException("CDP timeout: " + method))))
                {
                    var resp = await tcs.Task.ConfigureAwait(false);
                    if (resp.ContainsKey("error") && resp["error"] != null)
                    {
                        throw new InvalidOperationException("CDP error in " + method + ": " + _json.Serialize(resp["error"]));
                    }
                    return resp.ContainsKey("result") ? (resp["result"] as Dictionary<string, object>) ?? new Dictionary<string, object>() : new Dictionary<string, object>();
                }
            }
        }

        public async Task<string> EvalJsonAsync(string jsExpr, int timeoutMs = 10000)
        {
            var res = await SendAsync("Runtime.evaluate", new Dictionary<string, object>
            {
                { "expression", jsExpr },
                { "returnByValue", true },
                { "awaitPromise", true }
            }, timeoutMs).ConfigureAwait(false);

            if (res.ContainsKey("result"))
            {
                var rObj = res["result"] as Dictionary<string, object>;
                if (rObj != null && rObj.ContainsKey("value") && rObj["value"] != null)
                {
                    return Convert.ToString(rObj["value"]);
                }
            }
            return "";
        }

        public async Task ClickPointAsync(double x, double y)
        {
            await SendAsync("Input.dispatchMouseEvent", new Dictionary<string, object>
            {
                { "type", "mouseMoved" }, { "x", x }, { "y", y }
            }).ConfigureAwait(false);
            await SendAsync("Input.dispatchMouseEvent", new Dictionary<string, object>
            {
                { "type", "mousePressed" }, { "x", x }, { "y", y }, { "button", "left" }, { "clickCount", 1 }
            }).ConfigureAwait(false);
            await SendAsync("Input.dispatchMouseEvent", new Dictionary<string, object>
            {
                { "type", "mouseReleased" }, { "x", x }, { "y", y }, { "button", "left" }, { "clickCount", 1 }
            }).ConfigureAwait(false);
        }

        public void Dispose()
        {
            try { _ws.Dispose(); } catch { }
            try { _sendLock.Dispose(); } catch { }
        }
    }
}
