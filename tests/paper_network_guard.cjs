const net = require("node:net");

const originalConnect = net.Socket.prototype.connect;
const loopbackHosts = new Set(["127.0.0.1", "::1", "localhost"]);

function hostFrom(args) {
  const [first] = args;
  if (typeof first === "object" && first !== null) {
    return first.host ?? first.hostname;
  }
  if (typeof first === "number") {
    return args[1];
  }
  return undefined;
}

net.Socket.prototype.connect = function guardedConnect(...args) {
  const host = hostFrom(args);
  if (host !== undefined && !loopbackHosts.has(String(host))) {
    throw new Error(
      `Paper safety guard blocked Node network access to ${host}; ` +
        "mock the RPC or use the loopback status server.",
    );
  }
  return originalConnect.apply(this, args);
};