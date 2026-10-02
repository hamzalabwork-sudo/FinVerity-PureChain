// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

/// @title FinVerityRegistry
/// @notice Permissioned registry of cryptographic commitments to off-chain financial audit evidence.
///         Only the SHA-256 commitment H_i and minimal metadata are stored; the evidence package
///         E_i itself stays in off-chain storage.
contract FinVerityRegistry {
    struct Record {
        bytes32 evidenceHash; // H_i = SHA256(Canon(E_i))
        bytes32 metaHash;     // commitment to authorization / validation metadata (A_i, V_i)
        address submitter;    // account that submitted the record
        address approver;     // account whose signature S_i authorized the record
        uint64 timestamp;     // t_i, block timestamp at inclusion
        uint64 blockNumber;
        uint8 level;          // evidence complexity level (1-4)
    }

    address public immutable admin;
    mapping(address => bool) public isSubmitter;
    mapping(address => bool) public isApprover;
    mapping(bytes32 => Record) private records;
    uint256 public recordCount;

    // secp256k1n / 2, used to reject malleable signatures
    uint256 private constant HALF_N = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    event EvidenceRecorded(
        bytes32 indexed recordId,
        bytes32 evidenceHash,
        address indexed submitter,
        address indexed approver,
        uint64 timestamp
    );
    event RolesUpdated(address indexed account, bool submitter, bool approver);

    error NotAdmin();
    error NotSubmitter();
    error AlreadyRecorded(bytes32 recordId);
    error InvalidApproval();

    constructor() {
        admin = msg.sender;
        isSubmitter[msg.sender] = true;
    }

    function setRoles(address[] calldata accounts, bool submitter, bool approver) external {
        if (msg.sender != admin) revert NotAdmin();
        for (uint256 i = 0; i < accounts.length; i++) {
            isSubmitter[accounts[i]] = submitter;
            isApprover[accounts[i]] = approver;
            emit RolesUpdated(accounts[i], submitter, approver);
        }
    }

    /// @notice Digest the approver signs (EIP-191 personal_sign over a domain-bound struct hash).
    function approvalDigest(bytes32 recordId, bytes32 evidenceHash, bytes32 metaHash, uint8 level)
        public
        view
        returns (bytes32)
    {
        bytes32 inner = keccak256(abi.encode(block.chainid, address(this), recordId, evidenceHash, metaHash, level));
        return keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", inner));
    }

    function recordEvidence(
        bytes32 recordId,
        bytes32 evidenceHash,
        bytes32 metaHash,
        uint8 level,
        bytes calldata approval
    ) external {
        if (!isSubmitter[msg.sender]) revert NotSubmitter();
        if (records[recordId].timestamp != 0) revert AlreadyRecorded(recordId);
        address approver = _recover(approvalDigest(recordId, evidenceHash, metaHash, level), approval);
        if (approver == address(0) || !isApprover[approver]) revert InvalidApproval();

        records[recordId] = Record({
            evidenceHash: evidenceHash,
            metaHash: metaHash,
            submitter: msg.sender,
            approver: approver,
            timestamp: uint64(block.timestamp),
            blockNumber: uint64(block.number),
            level: level
        });
        unchecked { recordCount++; }
        emit EvidenceRecorded(recordId, evidenceHash, msg.sender, approver, uint64(block.timestamp));
    }

    // ------------------------------------------------------------------ batched commitments
    struct Batch {
        bytes32 merkleRoot;   // root over leaves keccak256(recordId, evidenceHash), sorted-pair hashing
        bytes32 metaHash;
        address submitter;
        address approver;
        uint64 timestamp;
        uint64 blockNumber;
        uint32 count;
        uint8 level;
    }

    mapping(bytes32 => Batch) private batches;
    uint256 public batchCount;

    event BatchRecorded(bytes32 indexed batchId, bytes32 merkleRoot, uint32 count, address indexed submitter,
                        address indexed approver, uint64 timestamp);

    /// @notice Commit k evidence records with one transaction (one Merkle root). The approver signs
    ///         approvalDigest(batchId, merkleRoot, metaHash, level).
    function recordBatch(
        bytes32 batchId,
        bytes32 merkleRoot,
        uint32 count,
        bytes32 metaHash,
        uint8 level,
        bytes calldata approval
    ) external {
        if (!isSubmitter[msg.sender]) revert NotSubmitter();
        if (batches[batchId].timestamp != 0) revert AlreadyRecorded(batchId);
        address approver = _recover(approvalDigest(batchId, merkleRoot, metaHash, level), approval);
        if (approver == address(0) || !isApprover[approver]) revert InvalidApproval();
        batches[batchId] = Batch({
            merkleRoot: merkleRoot, metaHash: metaHash, submitter: msg.sender, approver: approver,
            timestamp: uint64(block.timestamp), blockNumber: uint64(block.number), count: count, level: level
        });
        unchecked { batchCount++; recordCount += count; }
        emit BatchRecorded(batchId, merkleRoot, count, msg.sender, approver, uint64(block.timestamp));
    }

    function getBatch(bytes32 batchId) external view returns (Batch memory) {
        return batches[batchId];
    }

    /// @notice On-chain inclusion check of one record in a committed batch.
    function verifyInclusion(bytes32 batchId, bytes32 recordId, bytes32 evidenceHash, bytes32[] calldata proof)
        external
        view
        returns (bool)
    {
        Batch storage b = batches[batchId];
        if (b.timestamp == 0) return false;
        bytes32 h = keccak256(abi.encodePacked(recordId, evidenceHash));
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 p = proof[i];
            h = h < p ? keccak256(abi.encodePacked(h, p)) : keccak256(abi.encodePacked(p, h));
        }
        return h == b.merkleRoot;
    }

    function getRecord(bytes32 recordId) external view returns (Record memory) {
        return records[recordId];
    }

    /// @notice Verify(E_i') = 1 iff H(E_i') equals the recorded commitment.
    function verifyEvidence(bytes32 recordId, bytes32 candidateHash) external view returns (bool) {
        Record storage r = records[recordId];
        return r.timestamp != 0 && r.evidenceHash == candidateHash;
    }

    function _recover(bytes32 digest, bytes calldata sig) private pure returns (address) {
        if (sig.length != 65) return address(0);
        bytes32 r = bytes32(sig[0:32]);
        bytes32 s = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        if (v < 27) v += 27;
        if (uint256(s) > HALF_N || (v != 27 && v != 28)) return address(0);
        return ecrecover(digest, v, r, s);
    }
}
