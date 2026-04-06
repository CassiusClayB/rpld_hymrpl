#ifndef _RPL_H_

/*
 * NOTE: the contents of this file are an interpretation of RFC6550.
 *       no copyright is asserted on this file, as it transcribes
 *       a public specification.
 * It comes from https://github.com/mcr/unstrung/blob/master/include/rpl.h
 *
 * HyMRPL extensions: MOP=6 (hybrid mode) added as experimental value.
 * Per RFC6550 Section 6.3.1, MOP values 4-7 are unassigned/experimental.
 */

#define PACKED __attribute__((packed))

#define ND_RPL_MESSAGE 155  /* 0x9B */

enum ND_RPL_CODE {
    ND_RPL_DAG_IS=0x00,
    ND_RPL_DAG_IO=0x01,
    ND_RPL_DAO   =0x02,
    ND_RPL_DAO_ACK=0x03,
    ND_RPL_SEC_DAG_IS = 0x80,
    ND_RPL_SEC_DAG_IO = 0x81,
    ND_RPL_SEC_DAG    = 0x82,
    ND_RPL_SEC_DAG_ACK= 0x83,
    ND_RPL_SEC_CONSIST= 0x84,
};

enum ND_RPL_DIO_FLAGS {
        ND_RPL_DIO_GROUNDED = 0x80,
        ND_RPL_DIO_DATRIG   = 0x40,
        ND_RPL_DIO_DASUPPORT= 0x20,
        ND_RPL_DIO_RES4     = 0x10,
        ND_RPL_DIO_RES3     = 0x08,
        ND_RPL_DIO_PRF_MASK = 0x07,
};

#define DAGID_LEN 16

struct nd_rpl_security {
    u_int8_t  rpl_sec_t_reserved;
    u_int8_t  rpl_sec_algo;
    u_int16_t rpl_sec_kim_lvl_flags;
    u_int32_t rpl_sec_counter;
    u_int8_t  rpl_sec_ki[0];
} PACKED;

struct nd_rpl_opt {
    u_int8_t type;
    u_int8_t len;
} PACKED;

struct nd_rpl_dis {
    u_int8_t rpl_dis_flags;
    u_int8_t rpl_dis_reserved;
} PACKED;

struct rpl_dis_solicitedinfo {
    u_int8_t rpl_dis_type;
    u_int8_t rpl_dis_len;
    u_int8_t rpl_dis_instanceid;
    u_int8_t rpl_dis_flags;
    u_int8_t rpl_dis_dagid[DAGID_LEN];
    u_int8_t rpl_dis_versionnum;
} PACKED;
#define RPL_DIS_SI_V      (1 << 7)
#define RPL_DIS_SI_I      (1 << 6)
#define RPL_DIS_SI_D      (1 << 5)
#define RPL_DIS_SI_FLAGS  ((1 << 5)-1)

struct nd_rpl_dio {
    u_int8_t  rpl_instanceid;
    u_int8_t  rpl_version;
    u_int16_t rpl_dagrank;
    u_int8_t  rpl_mopprf;   /* bit 7=G, 5-3=MOP, 2-0=PRF */
    u_int8_t  rpl_dtsn;
    u_int8_t  rpl_flags;
    u_int8_t  rpl_resv1;
    struct in6_addr  rpl_dagid;
} PACKED;
#define RPL_DIO_GROUND_FLAG 0x80
#define RPL_DIO_MOP_SHIFT   3
#define RPL_DIO_MOP_MASK    (7 << RPL_DIO_MOP_SHIFT)
#define RPL_DIO_PRF_SHIFT   0
#define RPL_DIO_PRF_MASK    (7 << RPL_DIO_PRF_SHIFT)
#define RPL_DIO_GROUNDED(X) ((X)&RPL_DIO_GROUND_FLAG)
#define RPL_DIO_MOP(X)      (enum RPL_DIO_MOP)(((X)&RPL_DIO_MOP_MASK) >> RPL_DIO_MOP_SHIFT)
#define RPL_DIO_PRF(X)      (((X)&RPL_DIO_PRF_MASK) >> RPL_DIO_PRF_SHIFT)

enum RPL_DIO_MOP {
        RPL_DIO_NO_DOWNWARD_ROUTES_MAINT = 0x0,
        RPL_DIO_NONSTORING = 0x1,
        RPL_DIO_STORING_NO_MULTICAST = 0x2,
        RPL_DIO_STORING_MULTICAST    = 0x3,
        /* HyMRPL: experimental hybrid mode (RFC6550 allows values 4-7) */
        RPL_DIO_HYBRID               = 0x6,
};

/* HyMRPL: node functional profile */
#define HYMRPL_CLASS_S  0   /* storing-like behavior */
#define HYMRPL_CLASS_N  1   /* non-storing-like behavior */

enum RPL_SUBOPT {
        RPL_OPT_PAD0        = 0,
        RPL_OPT_PADN        = 1,
        RPL_DIO_METRICS     = 2,
        RPL_DIO_ROUTINGINFO = 3,
        RPL_DIO_CONFIG      = 4,
        RPL_DAO_RPLTARGET   = 5,
        RPL_DAO_TRANSITINFO = 6,
        RPL_DIS_SOLICITEDINFO=7,
        RPL_DIO_DESTPREFIX  = 8,
        RPL_DAO_RPLTARGET_DESC=9,
};

struct rpl_dio_genoption {
    u_int8_t rpl_dio_type;
    u_int8_t rpl_dio_len;
    u_int8_t rpl_dio_data[0];
} PACKED;

#define RPL_DIO_LIFETIME_INFINITE   0xffffffff
#define RPL_DIO_LIFETIME_DISCONNECT 0

#define RPL_DIO_PREFIX_AUTONOMOUS_ADDR_CONFIG_FLAG  0x40
#define RPL_DIO_PREFIX_AUTONOMOUS_ADDR_CONFIG_SHIFT 6
#define RPL_DIO_PREFIX_AUTONOMOUS_ADDR_CONFIG_MASK  (1 << RPL_DIO_PREFIX_AUTONOMOUS_ADDR_CONFIG_SHIFT)

struct rpl_dio_destprefix {
    u_int8_t rpl_dio_type;
    u_int8_t rpl_dio_len;
    u_int8_t rpl_dio_prefixlen;
    u_int8_t rpl_dio_prf;
    u_int32_t rpl_dio_route_lifetime;
    struct in6_addr rpl_dio_prefix;
} PACKED;

struct nd_rpl_dao {
    u_int8_t  rpl_instanceid;
    u_int8_t  rpl_flags;
    u_int8_t  rpl_resv;
    u_int8_t  rpl_daoseq;
    struct in6_addr rpl_dagid;
} PACKED;

#define RPL_DAO_K_SHIFT   7
#define RPL_DAO_K_MASK    (1 << RPL_DAO_K_SHIFT)
#define RPL_DAO_K(X)      (((X)&RPL_DAO_K_MASK) >> RPL_DAO_K_SHIFT)
#define RPL_DAO_D_SHIFT   6
#define RPL_DAO_D_MASK    (1 << RPL_DAO_D_SHIFT)
#define RPL_DAO_D(X)      (((X)&RPL_DAO_D_MASK) >> RPL_DAO_D_SHIFT)

struct rpl_dao_target {
    u_int8_t rpl_dao_type;
    u_int8_t rpl_dao_len;
    u_int8_t rpl_dao_flags;
    u_int8_t rpl_dao_prefixlen;
    struct in6_addr rpl_dao_prefix;
} PACKED;

struct rpl_dao_transit {
    u_int8_t type;
    u_int8_t len;
    u_int8_t flags;
    u_int8_t path_ctrl;
    u_int8_t seq;
    u_int8_t lifetime;
    struct in6_addr parent;
} PACKED;

struct nd_rpl_daoack {
    u_int8_t  rpl_instanceid;
    u_int8_t  rpl_flags;
    u_int8_t  rpl_daoseq;
    u_int8_t  rpl_status;
    struct in6_addr  rpl_dagid;
} PACKED;
#define RPL_DAOACK_D_SHIFT   7
#define RPL_DAOACK_D_MASK    (1 << RPL_DAOACK_D_SHIFT)
#define RPL_DAOACK_D(X)      (((X)&RPL_DAOACK_D_MASK) >> RPL_DAOACK_D_SHIFT)

#define _RPL_H_
#endif /* _RPL_H_ */
