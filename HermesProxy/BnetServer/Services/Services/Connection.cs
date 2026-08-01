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

        // TEMP EXPERIMENT - ContentHandleArray/BinaryContentHandleArray were never populated,
        // leaving them null. Wireshark capture showed the client subsequently querying real
        // Blizzard CDN servers for an all-zero config hash (GET /tpr/wow/config/00/00/000...000),
        // which fails and appears to cause the ~45s hang/BLZ51903006 disconnect. Testing whether
        // supplying the client's own real, already-cached CDN Key (from its local .build.info)
        // here changes this behavior.
        var contentHandle = new ContentHandle();
        contentHandle.Region = 1; // matches the 'us' region row in .build.info
        contentHandle.Usage = 0;  // unknown enum meaning - guess, first attempt
        contentHandle.Hash = Google.Protobuf.ByteString.CopyFrom(Convert.FromHexString("72c730bef365effe8a1373203e9c8c56"));

        response.ContentHandleArray = new ConnectionMeteringContentHandles();
        response.ContentHandleArray.ContentHandle.Add(contentHandle);
        Console.WriteLine("[CONNECT DEBUG] Populated ContentHandleArray with real CDN Key from .build.info");

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
