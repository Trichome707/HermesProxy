// Copyright (c) CypherCore <http://github.com/CypherCore> All rights reserved.
// Licensed under the GNU GENERAL PUBLIC LICENSE. See LICENSE file in the project root for full license information.

using Bgs.Protocol;
using Bgs.Protocol.Connection.V1;
using Framework.Constants;
using System;

namespace BNetServer.Services;

public partial class BnetServices
{
    [Service(ServiceRequirement.Unauthorized, OriginalHash.ConnectionService, 1)]
    BattlenetRpcErrorCode HandleConnect(ConnectRequest request, ConnectResponse response)
    {
        response.ServerId = new ProcessId();
        response.ServerId.Label = (uint)Environment.ProcessId;
        response.ServerId.Epoch = (uint)Time.UnixTime;
        response.ServerTime = (ulong)Time.UnixTimeMilliseconds;

        response.UseBindlessRpc = request.UseBindlessRpc;

        // TEMP EXPERIMENT - the 2.5.6 client's ConnectRequest never supplies client_id
        // (confirmed live: "request.ClientId was null"). Real Battle.net/TrinityCore behavior
        // is to synthesize a client identity when the request doesn't supply one, rather than
        // skip it - matching the secondary research's note that ciid is generated "from the
        // session ID and session creation time" when no client_id is present.
        if (request.ClientId != null)
        {
            response.ClientId.MergeFrom(request.ClientId);
        }
        else
        {
            response.ClientId = new ProcessId();
            response.ClientId.Label = (uint)GetSession().GetHashCode();
            response.ClientId.Epoch = (uint)Time.UnixTime;
            Console.WriteLine($"[CONNECT DEBUG] request.ClientId was null - synthesized ClientId Label={response.ClientId.Label:X8} Epoch={response.ClientId.Epoch:X8}");
        }

        response.Ciid = $"{response.ServerId.Label:X8}{response.ServerId.Epoch:X8}-{response.ClientId.Label:X8}{response.ClientId.Epoch:X8}";
        Console.WriteLine($"[CONNECT DEBUG] Set Ciid = {response.Ciid}");

        return BattlenetRpcErrorCode.Ok;
    }

    [Service(ServiceRequirement.Always, OriginalHash.ConnectionService, 5)]
    BattlenetRpcErrorCode HandleKeepAlive(NoData request)
    {
        return BattlenetRpcErrorCode.Ok;
    }

    [Service(ServiceRequirement.Always, OriginalHash.ConnectionService, 7)]
    BattlenetRpcErrorCode HandleRequestDisconnect(DisconnectRequest request)
    {
        // TEMP DEBUG - print exactly why the client is telling us to disconnect.
        Console.WriteLine($"[DISCONNECT DEBUG] Client-reported ErrorCode = {request.ErrorCode} (0x{request.ErrorCode:X})");

        if (GetSession() != null && GetSession().AuthClient != null)
            GetSession().AuthClient.Disconnect();

        var disconnectNotification = new DisconnectNotification();
        disconnectNotification.ErrorCode = request.ErrorCode;
        SendRequest(OriginalHash.ConnectionService, 4, disconnectNotification);

        CloseSocket();

        return BattlenetRpcErrorCode.Ok;
    }
}
